"""Debug replay: run a script once with one user and compare the replay with the recording.

The equivalent of replaying in LoadRunner's VuGen (or JMeter's View Results Tree). Every
request is labelled "#<index> ..." in the plan, so results map back to the recorded requests.
A Debug Sampler after each request that feeds a correlation records the variables it set.

For each request the summary lists what went wrong and the most likely cause (requirements
8.7 and 8.8): unresolved ${variables}, extractors that found nothing, status codes that differ
from the recording, failed checks and connection errors.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import xmlsafe
from .correlation import mask

PROPERTIES = (
    "-Jjmeter.save.saveservice.output_format=xml",
    "-Jjmeter.save.saveservice.response_data=true",
    "-Jjmeter.save.saveservice.samplerData=true",
    "-Jjmeter.save.saveservice.requestHeaders=true",
    "-Jjmeter.save.saveservice.responseHeaders=true",
    "-Jjmeter.save.saveservice.url=true",
    "-Jjmeter.save.saveservice.assertion_results=all",
    "-Jjmeter.save.saveservice.response_data.on_error=false",
)
KEEP_RUNS = 10
MAX_BODY_CHARS = 256_000
SENSITIVE = re.compile(r"^(authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token)\s*:",
                       re.IGNORECASE | re.MULTILINE)
_LABEL = re.compile(r"^#(\d+) ")
_VARIABLES = re.compile(r"^\[variables after #(\d+)\]$")
_UNRESOLVED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ID_SEGMENT = re.compile(r"/(\d{3,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[A-Za-z0-9_-]{16,})(/|$|\?)",
                         re.IGNORECASE)


def _text(sample: ET.Element, tag: str) -> str:
    el = sample.find(tag)
    return xmlsafe.visible(el.text or "") if el is not None else ""


def parse_results(path: Path) -> list[dict[str, Any]]:
    """Top-level samples of an XML results file, in run order."""
    if not path.exists() or not path.stat().st_size:
        return []
    raw = path.read_bytes()
    if b"</testResults>" not in raw[-64:]:
        raw += b"</testResults>"            # a stopped run can leave the file unterminated
    root = xmlsafe.parse(raw)
    samples = []
    for el in root:
        if el.tag not in ("httpSample", "sample"):
            continue
        assertions = [{
            "name": _text(a, "name"),
            "failed": _text(a, "failure") == "true" or _text(a, "error") == "true",
            "message": _text(a, "failureMessage"),
        } for a in el.findall("assertionResult")]
        samples.append({
            "label": xmlsafe.visible(el.get("lb", "")),
            "success": el.get("s") == "true",
            "status": el.get("rc", ""),
            "message": el.get("rm", ""),
            "elapsed_ms": int(el.get("t", "0") or 0),
            "latency_ms": int(el.get("lt", "0") or 0),
            "connect_ms": int(el.get("ct", "0") or 0),
            "bytes": int(el.get("by", "0") or 0),
            "sent_bytes": int(el.get("sby", "0") or 0),
            "timestamp": int(el.get("ts", "0") or 0),
            "url": _text(el, "java.net.URL"),
            "method": _text(el, "method"),
            "request_headers": _text(el, "requestHeader"),
            "request_body": _text(el, "queryString"),
            "sampler_data": _text(el, "samplerData"),
            "response_headers": _text(el, "responseHeader"),
            "response_body": _text(el, "responseData"),
            "assertions": assertions,
        })
    return samples


def _variables(body: str) -> dict[str, str]:
    out = {}
    for line in body.splitlines():
        name, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            out[name] = value
    return out


def _diagnose(result: dict[str, Any], *, unresolved: list[str], not_found: list[tuple[str, int]]) -> dict[str, str] | None:
    status = result["status"]
    code = int(status) if status.isdigit() else None
    failed_checks = [a for a in result["assertions"] if a["failed"]]
    body = (result.get("response_body") or "").lower()
    if status.startswith("Non HTTP"):
        return {"cause": f"The target could not be reached ({result['message'][:200]}).",
                "fix": "Check the host and port, that the application is running, and any firewall or proxy.",
                "where": "requests"}
    if unresolved:
        names = ", ".join(f"${{{v}}}" for v in unresolved)
        return {"cause": f"{names} had no value, so the request sent the text literally.",
                "fix": "Add a correlation or a parameter with that name, or remove it from the request.",
                "where": "correlation"}
    if not_found:
        var, source = not_found[0]
        return {"cause": f"The correlation for ${{{var}}} found nothing in the response of request #{source}.",
                "fix": "Open that response below and adjust the correlation's expression (Correlation tab).",
                "where": "correlation"}
    if code in (401, 403):
        if "csrf" in body or "xsrf" in body:
            return {"cause": "The CSRF token was missing or stale.",
                    "fix": "Correlate the CSRF token from the page or cookie that issues it (Correlation tab).",
                    "where": "correlation"}
        return {"cause": "Not authenticated: the login token or session cookie was not sent or not accepted.",
                "fix": "Check that the login request passed and that its token is correlated into this request.",
                "where": "correlation"}
    if code in (400, 422):
        return {"cause": "The server rejected the request data.",
                "fix": "A dynamic value may still be hard-coded: correlate or parameterize it.",
                "where": "parameters"}
    if code == 404 and _ID_SEGMENT.search(result.get("url") or ""):
        return {"cause": "The recorded ID in the URL does not exist in this run.",
                "fix": "Correlate the ID from the response that creates it, or parameterize it.",
                "where": "correlation"}
    if code is not None and code >= 500:
        return {"cause": f"The server failed with HTTP {code}.",
                "fix": "Check the server logs; often a value in the request is wrong for this run.",
                "where": "requests"}
    if failed_checks:
        return {"cause": f"A check failed: {failed_checks[0]['message'] or failed_checks[0]['name']}",
                "fix": "The response differs from the recording. An error page or a login redirect "
                       "usually means an earlier step failed.",
                "where": "checks"}
    if result.get("recorded_status") and code and code != result["recorded_status"]:
        return {"cause": f"The recording got HTTP {result['recorded_status']}, the replay got {code}.",
                "fix": "Compare the response with the recording.", "where": "requests"}
    return None


def summarize(path: Path, script: dict[str, Any], view: dict[str, Any], built_unresolved: dict[str, list[int]],
              warnings: list[str]) -> dict[str, Any]:
    """Per-request comparison with the recording, diagnosis and the first failure."""
    samples = parse_results(path)
    design = script.get("design") or {}
    correlations = design.get("correlations") or []
    source_of = {c["variable"]: c["source"] for c in correlations}
    interesting = set(source_of) | {p["name"] for p in design.get("parameters") or []}
    interesting |= {c for f in design.get("data_files") or [] for c in f.get("columns", [])}
    interesting |= set(script.get("variables") or [])

    by_index: dict[int, dict[str, Any]] = {}
    variables: dict[str, str] = {}
    transactions = []
    for s in samples:
        m = _LABEL.match(s["label"])
        v = _VARIABLES.match(s["label"])
        if m:
            by_index.setdefault(int(m.group(1)), s)
        elif v:
            variables.update({k: val for k, val in _variables(s["response_body"]).items() if k in interesting})
        else:
            transactions.append({"name": s["label"], "success": s["success"], "elapsed_ms": s["elapsed_ms"]})

    items = []
    first_failure = None
    tx_of = {i: t["name"] for t in view["transactions"] for i in t["items"]}
    for tx in view["transactions"]:
        for index in tx["items"]:
            item = script["items"][index]
            s = by_index.get(index)
            row: dict[str, Any] = {"index": index, "label": item["label"], "transaction": tx_of.get(index),
                                   "recorded_status": item.get("status"), "issues": []}
            if s is None:
                row.update(state="not_run", status=None, success=False, elapsed_ms=None, diagnosis=None)
                row["issues"].append("Not run (an earlier error stopped the iteration, or it is in a logic controller)")
                items.append(row)
                continue
            sent = f"{s['url']}\n{s['request_body']}\n{s['request_headers']}"
            unresolved = sorted(set(_UNRESOLVED.findall(sent)))
            not_found = [(var, source_of[var]) for var in sorted(set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)_NOT_FOUND", sent)))
                         if var in source_of]
            row.update(state="ran", status=s["status"], success=s["success"], elapsed_ms=s["elapsed_ms"],
                       bytes=s["bytes"], url=s["url"], method=s["method"],
                       checks=[{"name": a["name"], "failed": a["failed"], "message": a["message"]} for a in s["assertions"]])
            row["response_body"] = s["response_body"][:2000]
            if unresolved:
                row["issues"].append("Unresolved: " + ", ".join(f"${{{v}}}" for v in unresolved))
            for var, source in not_found:
                row["issues"].append(f"${{{var}}} was not found in request #{source}'s response")
            if item.get("status") and s["status"].isdigit() and int(s["status"]) != item["status"]:
                row["issues"].append(f"Status {s['status']} (recorded {item['status']})")
            for a in s["assertions"]:
                if a["failed"]:
                    row["issues"].append(f"Check failed: {a['message'] or a['name']}")
            if not s["success"] and not row["issues"]:
                row["issues"].append(f"Failed: {s['status']} {s['message']}")
            row["diagnosis"] = _diagnose({**s, "recorded_status": item.get("status")}, unresolved=unresolved,
                                         not_found=not_found) if (row["issues"] or not s["success"]) else None
            del row["response_body"]
            if first_failure is None and (row["issues"] or not s["success"]):
                first_failure = index
            items.append(row)

    ran = [i for i in items if i["state"] == "ran"]
    return {
        "requests": len(items),
        "passed": sum(1 for i in ran if i["success"] and not i["issues"]),
        "failed": sum(1 for i in ran if not i["success"] or i["issues"]),
        "not_run": sum(1 for i in items if i["state"] == "not_run"),
        "first_failure": first_failure,
        "verdict": "pass" if items and all(i["state"] == "ran" and i["success"] and not i["issues"] for i in items) else "fail",
        "items": items,
        "transactions": transactions,
        "variables": {k: {"preview": mask(v) if len(v) > 12 else v, "not_found": v.endswith("_NOT_FOUND")}
                      for k, v in sorted(variables.items())},
        "unresolved": built_unresolved,
        "warnings": warnings,
    }


def sample_detail(path: Path, index: int, *, reveal: bool) -> dict[str, Any] | None:
    """Full request and response of one request in a debug run."""
    for s in parse_results(path):
        m = _LABEL.match(s["label"])
        if m and int(m.group(1)) == index:
            detail = dict(s)
            if not reveal:
                for key in ("request_headers", "response_headers"):
                    detail[key] = _mask_headers(detail[key])
            truncated = len(detail["response_body"]) > MAX_BODY_CHARS
            detail["response_body"] = detail["response_body"][:MAX_BODY_CHARS]
            detail["response_truncated"] = truncated
            detail["revealed"] = reveal
            return detail
    return None


def _mask_headers(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if SENSITIVE.match(line):
            name = line.split(":", 1)[0]
            lines.append(f"{name}: ********")
        else:
            lines.append(line)
    return "\n".join(lines)
