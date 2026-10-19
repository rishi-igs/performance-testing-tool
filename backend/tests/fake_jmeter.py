#!/usr/bin/env python3
"""TEST DOUBLE for Apache JMeter. Not part of the product.

Understands just enough of the command line (-n -t -l -j -e -o -g -J) and of the .jmx
written by jmeter_plan_builder to send real HTTP requests and write a JTL CSV in
JMeter's format. It lets the pipeline be tested where JMeter is not installed.
Real runs use real JMeter (set JMETER_BIN).
"""
import csv
import http.client
import json
import re
import signal
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HEADER = ["timeStamp", "elapsed", "label", "responseCode", "responseMessage", "threadName", "dataType",
          "success", "failureMessage", "bytes", "sentBytes", "grpThreads", "allThreads", "URL",
          "Latency", "IdleTime", "Connect"]
stop = threading.Event()


def prop(el, name, default=""):
    for child in el.iter():
        if child.get("name") == name and child.text is not None:
            return child.text
    return default


def write_report(out: Path, jtl: Path):
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(f"<html><body>Fake JMeter report for {jtl.name}</body></html>")


def main(argv):
    args = {"-J": []}
    properties = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-J"):
            args["-J"].append(a)
            key, separator, value = a[2:].partition("=")
            if separator:
                properties[key] = value
        elif a in ("-n", "-e"):
            args[a] = True
        elif a in ("-t", "-l", "-j", "-o", "-g"):
            args[a] = argv[i + 1]
            i += 1
        i += 1

    if "-g" in args:  # report-only mode
        write_report(Path(args["-o"]), Path(args["-g"]))
        return 0

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    plan = ET.parse(args["-t"]).getroot()
    jtl = Path(args["-l"])
    fh = jtl.open("w", newline="")
    writer = csv.writer(fh)
    writer.writerow(HEADER)
    xml_output = properties.get("jmeter.save.saveservice.output_format") == "xml"
    xml_root = ET.Element("testResults") if xml_output else None
    xml_samples = []
    lock = threading.Lock()
    active = [0]
    t0 = time.time()

    def worker(group, sampler, headers, expected, assertions, start_time, number, group_users, loops):
        users, ramp, duration, delay = group
        thread_start = start_time + delay + (number * ramp / users if ramp else 0)
        time.sleep(max(0, thread_start - time.time()))
        if stop.is_set() or (duration and thread_start >= start_time + delay + duration):
            return
        with lock:
            active[0] += 1
        host, port = prop(sampler, "HTTPSampler.domain"), int(prop(sampler, "HTTPSampler.port"))
        proto, path = prop(sampler, "HTTPSampler.protocol"), prop(sampler, "HTTPSampler.path")
        method = prop(sampler, "HTTPSampler.method")
        timeout = int(prop(sampler, "HTTPSampler.response_timeout", "30000")) / 1000
        body = prop(sampler, "Argument.value", None)
        think = int(prop(sampler, "ConstantTimer.delay", "0") or 0) / 1000
        label = sampler.get("testname")
        iteration = 0
        while not stop.is_set() and (
            time.time() < start_time + delay + duration if duration else iteration < loops
        ):
            start = time.time()
            code, msg, size, fail = "", "", 0, ""
            response_body, response_headers = b"", ""
            assertion_results = []
            ok = False
            try:
                conn_cls = http.client.HTTPSConnection if proto == "https" else http.client.HTTPConnection
                conn = conn_cls(host, port, timeout=timeout)
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                response_body = data
                response_headers = "\n".join(f"{name}: {value}" for name, value in resp.getheaders())
                code, msg, size = str(resp.status), resp.reason, len(data)
                ok = not expected or any(
                    str(resp.status) == value or (
                        value.endswith(r"\d\d") and str(resp.status).startswith(value[0])
                    )
                    for value in expected
                )
                if expected:
                    assertion_results.append({
                        "name": "Expected status code",
                        "passed": ok,
                        "message": "" if ok else f"Expected one of {expected}; received {code}",
                    })
                if not ok:
                    fail = f"Test failed: code expected to equal /\n\n****** received  : [[[{code}]]]"
                for assertion in assertions:
                    passed = True
                    field = prop(assertion, "Assertion.test_field")
                    value = prop(assertion, "Asserion.test_strings")
                    test_type = int(prop(assertion, "Assertion.test_type", "0") or 0)
                    if field == "Assertion.response_data":
                        response_text = data.decode("utf-8", errors="replace")
                        if test_type == 2:
                            passed = value in response_text
                        elif test_type == 6:
                            passed = value not in response_text
                        elif test_type == 8:
                            passed = value == response_text
                        elif test_type == 16:
                            passed = re.search(value, response_text) is not None
                    elif field == "Assertion.response_headers":
                        passed = value.lower() in response_headers.lower()
                    assertion_results.append({
                        "name": assertion.get("testname", "Response assertion"),
                        "passed": passed,
                        "message": "" if passed else "Response assertion did not match",
                    })
                    if not passed:
                        ok = False
                        fail = f"Test failed: response assertion did not match: {assertion.get('testname', 'Assertion')}"
                conn.close()
            except Exception as exc:  # noqa: BLE001
                code = f"Non HTTP response code: java.net.{type(exc).__name__}"
                msg = f"Non HTTP response message: {exc}"
            elapsed = int((time.time() - start) * 1000)
            with lock:
                writer.writerow([int(start * 1000), elapsed, label, code, msg, f"Virtual users 1-{number + 1}",
                                 "text", str(ok).lower(), fail, size, 0, group_users, active[0],
                                 f"{proto}://{host}:{port}{path}", elapsed, 0, 0])
                fh.flush()
                if xml_output:
                    sample = ET.Element("httpSample", {
                        "t": str(elapsed), "lt": str(elapsed), "ct": "0",
                        "ts": str(int(start * 1000)), "s": str(ok).lower(), "lb": label,
                        "rc": code, "rm": msg, "by": str(size), "sby": "0", "tn": f"Virtual users 1-{number + 1}",
                        "dt": "text", "ng": str(group_users), "na": str(active[0]),
                        "url": f"{proto}://{host}:{port}{path}",
                    })
                    ET.SubElement(sample, "responseData").text = response_body.decode("utf-8", errors="replace")
                    sampler_data = f"{method} {proto}://{host}:{port}{path} HTTP/1.1\n"
                    sampler_data += "".join(f"{key}: {value}\n" for key, value in headers.items())
                    body = prop(sampler, "Argument.value", "")
                    if body:
                        sampler_data += "\n" + body
                    ET.SubElement(sample, "samplerData").text = sampler_data
                    ET.SubElement(sample, "requestHeader").text = "".join(f"{k}: {v}\n" for k, v in headers.items())
                    ET.SubElement(sample, "responseHeader").text = response_headers
                    ET.SubElement(sample, "failureMessage").text = fail
                    for result in assertion_results:
                        assertion_result = ET.SubElement(sample, "assertionResult")
                        ET.SubElement(assertion_result, "name").text = result["name"]
                        ET.SubElement(assertion_result, "failure").text = str(not result["passed"]).lower()
                        ET.SubElement(assertion_result, "error").text = "false"
                        ET.SubElement(assertion_result, "failureMessage").text = result["message"]
                    xml_samples.append(sample)
            if think:
                time.sleep(think)
            iteration += 1
        with lock:
            active[0] -= 1

    threads = []
    groups = plan.findall(".//ThreadGroup")
    for group in groups:
        parent_tree = next(tree for tree in plan.findall(".//hashTree") if group in list(tree))
        group_tree = list(parent_tree)[list(parent_tree).index(group) + 1]
        users = int(prop(group, "ThreadGroup.num_threads"))
        ramp = int(prop(group, "ThreadGroup.ramp_time") or 0)
        duration = int(prop(group, "ThreadGroup.duration", "0") or 0)
        delay = int(prop(group, "ThreadGroup.delay") or 0)
        sampler = group_tree.find(".//HTTPSamplerProxy")
        sampler_tree = None
        for parent in plan.iter():
            children = list(parent)
            for pos, child in enumerate(children[:-1]):
                if child is sampler and children[pos + 1].tag == "hashTree":
                    sampler_tree = children[pos + 1]
        common_headers = {
            prop(h, "Header.name"): prop(h, "Header.value")
            for h in parent_tree.findall("./HeaderManager//elementProp[@elementType='Header']")
        }
        group_headers = {
            prop(h, "Header.name"): prop(h, "Header.value")
            for h in group_tree.findall("./HeaderManager//elementProp[@elementType='Header']")
        }
        sampler_headers = {
            prop(h, "Header.name"): prop(h, "Header.value")
            for h in sampler_tree.findall("./HeaderManager//elementProp[@elementType='Header']")
        } if sampler_tree is not None else {}
        headers = {**common_headers, **group_headers, **sampler_headers}
        expected, assertions = [], []
        if sampler_tree is not None:
            for assertion in sampler_tree.findall("ResponseAssertion"):
                field = prop(assertion, "Assertion.test_field")
                strings = assertion.findall("./collectionProp/stringProp")
                if field == "Assertion.response_code":
                    expected.extend(item.text or "" for item in strings)
                else:
                    assertions.append(assertion)
        loops = int(prop(group, "LoopController.loops", "1") or 1)
        threads.extend(
            threading.Thread(target=worker, args=((users, ramp, duration, delay), sampler, headers, expected,
                                                  assertions, t0, n, users, loops))
            for n in range(users)
        )
    [t.start() for t in threads]
    [t.join() for t in threads]
    fh.close()
    if xml_output and xml_root is not None:
        xml_root.extend(xml_samples)
        ET.ElementTree(xml_root).write(jtl, encoding="utf-8", xml_declaration=True)
    if "-o" in args:
        write_report(Path(args["-o"]), jtl)
    return 143 if stop.is_set() else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
