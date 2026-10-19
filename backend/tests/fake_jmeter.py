#!/usr/bin/env python3
"""TEST DOUBLE for Apache JMeter. Not part of the product.

Understands just enough of the command line (-n -t -l -j -e -o -g -J) and of the .jmx files
this tool writes to send real HTTP requests and write results in JMeter's CSV or XML format:
thread groups (users, ramp-up, duration, delay, loops), Transaction Controllers, HTTP requests
with Header Managers, a cookie jar, User Defined Variables, CSV Data Sets, Counters, JSON /
regex / boundary extractors, a few functions, Response/JSON/Duration/Size assertions and
Debug Samplers. Timers are ignored. It lets the pipeline be tested where JMeter is not
installed; real runs use real JMeter (set JMETER_BIN), and tests/test_real_jmeter.py runs the
same flows against it.
"""
import csv
import http.client
import json
import random
import re
import signal
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape

HEADER = ["timeStamp", "elapsed", "label", "responseCode", "responseMessage", "threadName", "dataType",
          "success", "failureMessage", "bytes", "sentBytes", "grpThreads", "allThreads", "URL",
          "Latency", "IdleTime", "Connect"]
stop = threading.Event()
SAMPLERS = ("HTTPSamplerProxy", "DebugSampler")


def prop(el, name, default=""):
    for child in el:
        if child.get("name") == name and child.text is not None:
            return child.text
    return default


def deep_prop(el, name, default=""):
    for child in el.iter():
        if child.get("name") == name and child.text is not None:
            return child.text
    return default


def pairs(tree):
    children = list(tree)
    i = 0
    while i < len(children):
        el = children[i]
        sub = children[i + 1] if i + 1 < len(children) and children[i + 1].tag == "hashTree" else ET.Element("hashTree")
        if el.tag != "hashTree":
            yield el, sub
        i += 2 if i + 1 < len(children) and children[i + 1].tag == "hashTree" else 1


def enabled(el):
    return el.get("enabled", "true") != "false"


def write_report(out: Path, jtl: Path):
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(f"<html><body>Fake JMeter report for {jtl.name}</body></html>")


# ---- variables and functions ------------------------------------------------------------------

_FUNC = re.compile(r"\$\{__(\w+)\(([^()]*)\)\}")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def substitute(text, variables):
    if not text or "${" not in text:
        return text
    for _ in range(4):                          # nested: ${__urlencode(${x})}, ${__V(a__${i})}
        before = text
        text = _VAR.sub(lambda m: variables.get(m.group(1), m.group(0)), text)
        text = _FUNC.sub(lambda m: call(m.group(1), m.group(2), variables, m.group(0)), text)
        if text == before:
            break
    return text


def call(name, args, variables, original):
    parts = [a.replace("\\,", ",") for a in re.split(r"(?<!\\),", args)]
    if name == "UUID":
        return str(uuid.uuid4())
    if name == "Random":
        return str(random.randint(int(parts[0]), int(parts[1])))
    if name == "urlencode":
        return quote(parts[0], safe="")
    if name == "V":
        return variables.get(parts[0], original)
    if name == "RandomString":
        return "".join(random.choice(parts[1] or "abc") for _ in range(int(parts[0])))
    if name in ("time", "timeShift"):
        java = {"yyyy": "%Y", "MM": "%m", "dd": "%d", "HH": "%H", "mm": "%M", "ss": "%S"}
        fmt = parts[0] or ""
        moment = datetime.now()
        if name == "timeShift" and len(parts) > 2 and parts[2]:
            moment += timedelta(days=int(re.sub(r"[^-0-9]", "", parts[2]) or 0))
        if not fmt:
            return str(int(moment.timestamp() * 1000))
        for j, p in java.items():
            fmt = fmt.replace(j, p)
        return moment.strftime(fmt)
    return original


# ---- the plan -----------------------------------------------------------------------------------

class Plan:
    def __init__(self, path):
        self.path = Path(path)
        root = ET.parse(path).getroot()
        self.plan_el, self.plan_tree = next(pairs(root.find("hashTree")))
        self.variables = {}
        for arg in self.plan_el.iter("elementProp"):
            if arg.get("elementType") == "Argument":
                self.variables[prop(arg, "Argument.name")] = prop(arg, "Argument.value")
        self.csv_sets, self.counters, self.randoms = [], [], []
        self.cookies = any(el.tag == "CookieManager" and enabled(el) for el in root.iter())
        self.groups = [(el, sub) for el, sub in pairs(self.plan_tree) if el.tag.endswith("ThreadGroup") and enabled(el)]
        for el, _ in pairs(self.plan_tree):
            self._config(el)
        for _, sub in self.groups:
            for el, _ in pairs(sub):
                self._config(el)

    def _config(self, el):
        if not enabled(el):
            return
        if el.tag == "CSVDataSet":
            file = self.path.parent / prop(el, "filename")
            rows = list(csv.reader(file.open(encoding="utf-8"))) if file.exists() else []
            if prop(el, "ignoreFirstLine") == "true":
                rows = rows[1:]
            self.csv_sets.append({"names": prop(el, "variableNames").split(","), "rows": rows, "next": 0,
                                  "lock": threading.Lock()})
        elif el.tag == "CounterConfig":
            self.counters.append({"name": prop(el, "CounterConfig.name"), "value": int(prop(el, "CounterConfig.start") or 1),
                                  "incr": int(prop(el, "CounterConfig.incr") or 1), "lock": threading.Lock()})
        elif el.tag == "RandomVariableConfig":
            self.randoms.append((prop(el, "variableName"), int(prop(el, "minimumValue") or 0), int(prop(el, "maximumValue") or 0)))

    def steps(self, tree, chain):
        """Flat run order: samplers (with the hashTrees above them), and the end of each transaction."""
        out = []
        for el, sub in pairs(tree):
            if not enabled(el):
                continue
            if el.tag == "TransactionController":
                start = len(out)
                out += self.steps(sub, chain + [sub])
                out.append(("tc", el.get("testname"), start))
            elif el.tag in ("GenericController", "RecordingController"):
                out += self.steps(sub, chain + [sub])
            elif el.tag in SAMPLERS:
                out.append(("sample", el, sub, chain))
        return out


def headers_for(chain, sub):
    headers = {}
    for tree in chain + [sub]:
        for el, _ in pairs(tree):
            if el.tag == "HeaderManager" and enabled(el):
                for h in el.iter("elementProp"):
                    headers[prop(h, "Header.name")] = prop(h, "Header.value")
    return headers


def extract(el, body, headers_text):
    if el.tag == "JSONPostProcessor":
        name, path = prop(el, "JSONPostProcessor.referenceNames"), prop(el, "JSONPostProcessor.jsonPathExprs")
        try:
            value = json.loads(body)
            for key in re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\['([^']+)'\]", path.removeprefix("$")):
                value = value[int(key[1])] if key[1] else value[key[0] or key[2]]
            return name, str(value).lower() if isinstance(value, bool) else str(value)
        except (ValueError, KeyError, IndexError, TypeError):
            return name, prop(el, "JSONPostProcessor.defaultValues")
    if el.tag == "RegexExtractor":
        name = prop(el, "RegexExtractor.refname")
        source = headers_text if prop(el, "RegexExtractor.useHeaders") == "true" else body
        m = re.search(prop(el, "RegexExtractor.regex"), source)
        return name, m.group(1) if m else prop(el, "RegexExtractor.default")
    name = prop(el, "BoundaryExtractor.refname")
    source = headers_text if prop(el, "BoundaryExtractor.useHeaders") == "true" else body
    left, right = prop(el, "BoundaryExtractor.lboundary"), prop(el, "BoundaryExtractor.rboundary")
    i = source.find(left)
    j = source.find(right, i + len(left)) if i >= 0 else -1
    return name, source[i + len(left):j] if i >= 0 and j >= 0 else prop(el, "BoundaryExtractor.default")


def check(el, code, body, headers_text, elapsed):
    """Return a failure message, or '' when the assertion passes."""
    if el.tag == "ResponseAssertion":
        field = prop(el, "Assertion.test_field")
        kind = int(prop(el, "Assertion.test_type", "16"))
        strings = [s.text or "" for s in el.iter("stringProp") if s.get("name") not in (
            "Assertion.custom_message", "Assertion.test_field")]
        if field == "Assertion.response_code":
            return "" if code in strings else f"Test failed: code expected to equal /\n\n****** received  : [[[{code}]]]"
        target = headers_text if field == "Assertion.response_headers" else body
        found = any((re.search(s, target) if kind & 2 else s in target) for s in strings)
        ok = not found if kind & 4 else found
        return "" if ok else (prop(el, "Assertion.custom_message") or "Test failed: text expected to contain /")
    if el.tag == "JSONPathAssertion":
        try:
            data = json.loads(body)
            for key in re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]", prop(el, "JSON_PATH").removeprefix("$")):
                data = data[int(key[1])] if key[1] else data[key[0]]
        except (ValueError, KeyError, IndexError, TypeError):
            return f"No results for path: {prop(el, 'JSON_PATH')}"
        expected = prop(el, "EXPECTED_VALUE")
        actual = str(data).lower() if isinstance(data, bool) else str(data)
        if prop(el, "JSONVALIDATION") == "true" and actual != expected:
            return f"Value expected to be '{expected}', but found '{actual}'"
        return ""
    if el.tag == "DurationAssertion":
        limit = int(prop(el, "DurationAssertion.duration", "0") or 0)
        return f"The operation lasted too long: It took {elapsed} milliseconds" if limit and elapsed > limit else ""
    if el.tag == "SizeAssertion":
        limit = int(prop(el, "SizeAssertion.size", "0") or 0)
        return f"The result was the wrong size: It was {len(body)} bytes" if len(body.encode()) > limit else ""
    return ""


class Writer:
    def __init__(self, path, xml):
        self.fh = path.open("w", newline="", encoding="utf-8")
        self.xml = xml
        self.lock = threading.Lock()
        if xml:
            self.fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<testResults version="1.2">\n')
        else:
            self.csv = csv.writer(self.fh)
            self.csv.writerow(HEADER)

    def write(self, r):
        with self.lock:
            if self.xml:
                tag = "httpSample" if r.get("url") else "sample"
                asserts = "".join(
                    f"<assertionResult><name>{escape(a[0])}</name><failure>{str(bool(a[1])).lower()}</failure>"
                    f"<error>false</error><failureMessage>{escape(a[1])}</failureMessage></assertionResult>"
                    for a in r.get("assertions", []))
                self.fh.write(
                    f'<{tag} t="{r["elapsed"]}" lt="{r["elapsed"]}" ct="0" ts="{r["ts"]}" s="{str(r["ok"]).lower()}" '
                    f'lb="{escape(r["label"], {chr(34): "&quot;"})}" rc="{escape(r["code"])}" rm="{escape(r["msg"], {chr(34): "&quot;"})}" '
                    f'tn="{escape(r["thread"])}" by="{len(r.get("body", "").encode())}" sby="0">'
                    f'{asserts}<requestHeader class="java.lang.String">{escape(r.get("req_headers", ""))}</requestHeader>'
                    f'<responseHeader class="java.lang.String">{escape(r.get("resp_headers", ""))}</responseHeader>'
                    f'<responseData class="java.lang.String">{escape(r.get("body", ""))}</responseData>'
                    f'<method class="java.lang.String">{escape(r.get("method", ""))}</method>'
                    f'<queryString class="java.lang.String">{escape(r.get("req_body") or "")}</queryString>'
                    f'<java.net.URL>{escape(r.get("url", ""))}</java.net.URL></{tag}>\n')
            else:
                self.csv.writerow([r["ts"], r["elapsed"], r["label"], r["code"], r["msg"], r["thread"], "text",
                                   str(r["ok"]).lower(), r.get("failure", ""), len(r.get("body", "").encode()), 0,
                                   r["group_users"], r["active"], r.get("url", ""), r["elapsed"], 0, 0])
            self.fh.flush()

    def close(self):
        if self.xml:
            self.fh.write("</testResults>\n")
        self.fh.close()


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
    # Tests check the command line (for example -R for distributed engines).
    (Path(args["-l"]).parent / "fake_argv.json").write_text(json.dumps(argv), encoding="utf-8")

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    plan = Plan(args["-t"])
    xml = any(j.endswith("output_format=xml") for j in args["-J"]) and \
        [j for j in args["-J"] if "output_format=" in j][-1].endswith("xml")
    writer = Writer(Path(args["-l"]), xml)
    lock = threading.Lock()
    active = [0]
    t0 = time.time()

    def run_sample(el, sub, chain, variables, jar, thread_name, group_users):
        if el.tag == "DebugSampler":
            body = "JMeterVariables:\n" + "\n".join(f"{k}={v}" for k, v in sorted(variables.items()))
            return {"label": el.get("testname"), "code": "200", "msg": "OK", "ok": True, "body": body, "elapsed": 0,
                    "ts": int(time.time() * 1000), "thread": thread_name, "group_users": group_users,
                    "active": active[0]}
        host = substitute(prop(el, "HTTPSampler.domain"), variables)
        port = int(prop(el, "HTTPSampler.port") or 80)
        proto = prop(el, "HTTPSampler.protocol") or "http"
        path = substitute(prop(el, "HTTPSampler.path") or "/", variables)
        method = prop(el, "HTTPSampler.method") or "GET"
        timeout = int(prop(el, "HTTPSampler.response_timeout", "30000") or 30000) / 1000
        raw_body = deep_prop(el, "Argument.value", None) if prop(el, "HTTPSampler.postBodyRaw") == "true" else None
        body = substitute(raw_body, variables)
        headers = {k: substitute(v, variables) for k, v in headers_for(chain, sub).items()}
        if jar:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        start = time.time()
        code, msg, data, resp_headers = "", "", "", ""
        try:
            conn_cls = http.client.HTTPSConnection if proto == "https" else http.client.HTTPConnection
            conn = conn_cls(host, port, timeout=timeout)
            conn.request(method, path, body=body.encode() if body is not None else None, headers=headers)
            resp = conn.getresponse()
            data = resp.read().decode("utf-8", "replace")
            code, msg = str(resp.status), resp.reason
            resp_headers = f"HTTP/1.1 {resp.status} {resp.reason}\n" + "".join(f"{k}: {v}\n" for k, v in resp.getheaders())
            for k, v in resp.getheaders():
                if k.lower() == "set-cookie" and jar is not None:
                    name, _, rest = v.partition("=")
                    jar[name.strip()] = rest.split(";")[0]
            conn.close()
        except Exception as exc:  # noqa: BLE001
            code = f"Non HTTP response code: java.net.{type(exc).__name__}"
            msg = f"Non HTTP response message: {exc}"
        elapsed = int((time.time() - start) * 1000)
        assertions, ok = [], not code.startswith("Non HTTP") and code[:1] in "23"
        for child, _ in pairs(sub):
            if not enabled(child):
                continue
            if child.tag in ("JSONPostProcessor", "RegexExtractor", "BoundaryExtractor"):
                name, value = extract(child, data, resp_headers)
                variables[name] = value
            elif child.tag in ("ResponseAssertion", "JSONPathAssertion", "DurationAssertion", "SizeAssertion"):
                if child.tag == "ResponseAssertion" and prop(child, "Assertion.assume_success") == "true":
                    ok = not code.startswith("Non HTTP")
                failure = check(child, code, data, resp_headers, elapsed)
                assertions.append((child.get("testname"), failure))
        if any(f for _, f in assertions):
            ok = False
        return {"label": el.get("testname"), "code": code, "msg": msg, "ok": ok, "elapsed": elapsed,
                "ts": int(start * 1000), "thread": thread_name, "group_users": group_users, "active": active[0],
                "url": f"{proto}://{host}:{port}{path}", "method": method, "req_body": body,
                "req_headers": "".join(f"{k}: {v}\n" for k, v in headers.items()), "resp_headers": resp_headers,
                "body": data, "assertions": assertions, "failure": next((f for _, f in assertions if f), "")}

    def worker(group_el, group_tree, number, users):
        ramp = int(prop(group_el, "ThreadGroup.ramp_time") or 0)
        scheduler = prop(group_el, "ThreadGroup.scheduler") == "true"
        duration = int(prop(group_el, "ThreadGroup.duration") or 0)
        delay = int(prop(group_el, "ThreadGroup.delay") or 0)
        loops = int(deep_prop(group_el, "LoopController.loops", "-1") or -1)
        thread_start = t0 + delay + (number * ramp / users if ramp else 0)
        time.sleep(max(0, thread_start - time.time()))
        end = t0 + delay + duration if scheduler else float("inf")
        if stop.is_set() or (scheduler and thread_start >= end):
            return
        with lock:
            active[0] += 1
        variables = dict(plan.variables)
        steps = plan.steps(group_tree, [plan.plan_tree, group_tree])
        thread_name = f"{group_el.get('testname')} 1-{number + 1}"
        iteration = 0
        while not stop.is_set() and time.time() < end and (loops < 0 or iteration < loops):
            iteration += 1
            jar = {} if plan.cookies else None
            for c in plan.csv_sets:
                with c["lock"]:
                    row = c["rows"][c["next"] % len(c["rows"])] if c["rows"] else []
                    c["next"] += 1
                variables.update(dict(zip(c["names"], row)))
            for c in plan.counters:
                with c["lock"]:
                    variables[c["name"]] = str(c["value"])
                    c["value"] += c["incr"]
            for name, low, high in plan.randoms:
                variables[name] = str(random.randint(low, high))
            results = []
            marks = {}                    # step index -> number of results before it ran
            for position, step in enumerate(steps):
                marks[position] = len(results)
                if stop.is_set() or time.time() >= end:
                    break
                if step[0] == "sample":
                    r = run_sample(step[1], step[2], step[3], variables, jar, thread_name, users)
                    writer.write(r)
                    results.append(r)
                else:
                    _, label, start = step
                    inside = [r for r in results[marks.get(start, len(results)):] if r.get("url")]
                    if inside:
                        failing = sum(1 for r in inside if not r["ok"])
                        writer.write({"label": label, "code": "200" if not failing else "", "ok": not failing,
                                      "msg": f"Number of samples in transaction : {len(inside)}, number of failing samples : {failing}",
                                      "elapsed": sum(r["elapsed"] for r in inside), "ts": inside[0]["ts"],
                                      "thread": thread_name, "group_users": users, "active": active[0]})
        with lock:
            active[0] -= 1

    threads = []
    for group_el, group_tree in plan.groups:
        users = int(prop(group_el, "ThreadGroup.num_threads") or 1)
        threads.extend(threading.Thread(target=worker, args=(group_el, group_tree, n, users)) for n in range(users))
    [t.start() for t in threads]
    [t.join() for t in threads]
    writer.close()
    if "-o" in args:
        write_report(Path(args["-o"]), Path(args["-l"]))
    return 143 if stop.is_set() else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
