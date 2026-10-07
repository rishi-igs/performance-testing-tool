#!/usr/bin/env python3
"""TEST DOUBLE for Apache JMeter. Not part of the product.

Understands just enough of the command line (-n -t -l -j -e -o -g -J) and of the .jmx
written by jmeter_plan_builder to send real HTTP requests and write a JTL CSV in
JMeter's format. It lets the pipeline be tested where JMeter is not installed.
Real runs use real JMeter (set JMETER_BIN).
"""
import csv
import http.client
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
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-J"):
            args["-J"].append(a)
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
    tg = plan.find(".//ThreadGroup")
    users = int(prop(tg, "ThreadGroup.num_threads"))
    ramp = int(prop(tg, "ThreadGroup.ramp_time") or 0)
    duration = int(prop(tg, "ThreadGroup.duration"))
    sampler = plan.find(".//HTTPSamplerProxy")
    host, port = prop(sampler, "HTTPSampler.domain"), int(prop(sampler, "HTTPSampler.port"))
    proto, path = prop(sampler, "HTTPSampler.protocol"), prop(sampler, "HTTPSampler.path")
    method = prop(sampler, "HTTPSampler.method")
    timeout = int(prop(sampler, "HTTPSampler.response_timeout", "30000")) / 1000
    body = prop(sampler, "Argument.value", None)
    headers = {}
    for h in plan.findall(".//HeaderManager//elementProp[@elementType='Header']"):
        headers[prop(h, "Header.name")] = prop(h, "Header.value")
    expected = [int(e.text) for e in plan.findall(".//ResponseAssertion/collectionProp/stringProp")]
    think = int(prop(plan, "ConstantTimer.delay", "0") or 0) / 1000
    label = sampler.get("testname")

    jtl = Path(args["-l"])
    fh = jtl.open("w", newline="")
    writer = csv.writer(fh)
    writer.writerow(HEADER)
    lock = threading.Lock()
    active = [0]
    t0 = time.time()

    def worker(n):
        time.sleep(n * ramp / users if ramp else 0)
        with lock:
            active[0] += 1
        while not stop.is_set() and time.time() < t0 + duration:
            start = time.time()
            code, msg, size, fail = "", "", 0, ""
            ok = False
            try:
                conn_cls = http.client.HTTPSConnection if proto == "https" else http.client.HTTPConnection
                conn = conn_cls(host, port, timeout=timeout)
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                code, msg, size = str(resp.status), resp.reason, len(data)
                ok = resp.status in expected
                if not ok:
                    fail = f"Test failed: code expected to equal /\n\n****** received  : [[[{code}]]]"
                conn.close()
            except Exception as exc:  # noqa: BLE001
                code = f"Non HTTP response code: java.net.{type(exc).__name__}"
                msg = f"Non HTTP response message: {exc}"
            elapsed = int((time.time() - start) * 1000)
            with lock:
                writer.writerow([int(start * 1000), elapsed, label, code, msg, f"Virtual users 1-{n + 1}",
                                 "text", str(ok).lower(), fail, size, 0, users, active[0],
                                 f"{proto}://{host}:{port}{path}", elapsed, 0, 0])
                fh.flush()
            if think:
                time.sleep(think)
        with lock:
            active[0] -= 1

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(users)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    fh.close()
    if "-o" in args:
        write_report(Path(args["-o"]), jtl)
    return 143 if stop.is_set() else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
