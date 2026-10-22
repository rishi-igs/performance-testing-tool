"""HAR comparison validator (requirements 8.5): does the generated script reproduce the recording?

The cross-check against the browser's DevTools, done automatically. Each kept request of the
recording is compared with the request the plan will send:

  endpoint  method, host, path and query parameters
  headers   the ones that matter are still sent (browser-managed headers are ignored); cookies the
            recording sent are set by an earlier response, so the Cookie Manager can send them
  body      the same JSON fields or form fields, with dynamic values replaced by variables
  tokens    every dynamic-looking value is correlated, parameterized or marked static
  order     the requests run in the recorded order, and each correlation's source runs first

"Recorded" is the plan built without the script's design (no correlations, parameters or
replacements), so both sides go through the same plan builder. Values correlated during a HAR
import are already ${variables} on both sides; the recording itself is not stored.

Each difference is missing, extra, changed, unresolved or variable (a recorded value now sent as
a ${variable}, which is what correlation and parameterization should do). The result is Pass,
Pass with warnings or Fail; Fail means the replay is sure to go wrong.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..models.script import ScriptDesign
from . import script_builder
from .autocorrelate import sampler_request
from .correlation import _request_values, looks_dynamic, mask
from .jmeter_plan_builder import PlanTree

_VAR = re.compile(r"\$\{")
_BROWSER_HEADERS = {"user-agent", "accept", "accept-encoding", "accept-language", "cache-control", "pragma", "dnt",
                    "priority", "referer", "origin", "upgrade-insecure-requests", "te", "connection"}
_REQUIRED_HEADERS = {"authorization", "content-type", "x-csrf-token", "x-xsrf-token", "x-requested-with"}
SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "x-auth-token", "x-csrf-token",
                  "x-xsrf-token"}
_SECRET_PARTS = ("password", "passwd", "secret", "token", "apikey", "api_key", "api-key", "credential", "private_key")
_SECRET_WORDS = {"pass", "passcode", "pwd", "pin", "otp", "ssn", "cvv", "cvc", "jwt"}


def is_secret_name(name: str) -> bool:
    """A field or parameter whose value should be masked: password, token, api_key, pin, ..."""
    low = name.lower()
    if any(part in low for part in _SECRET_PARTS):
        return True
    words = re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", name).lower())
    return any(w in _SECRET_WORDS for w in words)
LABELS = {"pass": "Pass", "warn": "Pass with warnings", "fail": "Fail"}
_RANK = {"info": 0, "warning": 1, "fail": 2}


def _has_var(text: str | None) -> bool:
    return bool(text) and bool(_VAR.search(text))


def _variables_in(text: str) -> list[str]:
    return sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text or "")))


# ---- what a sampler sends ---------------------------------------------------------------------------

def _parents(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def _scope(tree: PlanTree, index: int, parents: dict[ET.Element, ET.Element]) -> list[ET.Element]:
    """The hashTrees whose config elements apply to a sampler, outermost first."""
    sampler, sub, holder = tree.samplers[index]
    chain, node = [sub, holder], holder
    while node in parents:
        node = parents[node]
        if node.tag == "hashTree":
            chain.append(node)
    return list(reversed(chain))


def _headers(scope: list[ET.Element]) -> list[list[str]]:
    """Headers from every Header Manager in scope; an inner one replaces a header of the same name."""
    merged: dict[str, list[str]] = {}
    for level in scope:
        for manager in level:
            if manager.tag != "HeaderManager" or manager.get("enabled", "true") == "false":
                continue
            for header in manager.iter("elementProp"):
                name = header.find("stringProp[@name='Header.name']")
                value = header.find("stringProp[@name='Header.value']")
                if name is not None and name.text:
                    merged[name.text.lower()] = [name.text, (value.text or "") if value is not None else ""]
    return list(merged.values())


def _defaults(scope: list[ET.Element]) -> dict[str, str]:
    found: dict[str, str] = {}
    for level in scope:
        for el in level:
            if el.tag == "ConfigTestElement" and el.get("guiclass") == "HttpDefaultsGui" and el.get("enabled", "true") != "false":
                for c in el:
                    key = (c.get("name") or "").removeprefix("HTTPSampler.")
                    if key in ("domain", "port", "protocol") and c.text:
                        found[key] = c.text
    return found


def _cookie_manager(scope: list[ET.Element]) -> bool:
    return any(el.tag == "CookieManager" and el.get("enabled", "true") != "false" for level in scope for el in level)


def request_of(tree: PlanTree, index: int, parents: dict[ET.Element, ET.Element]) -> dict[str, Any]:
    sampler, sub, _ = tree.samplers[index]
    scope = _scope(tree, index, parents)
    req = sampler_request(sampler, sub)
    prop = {(c.get("name") or "").removeprefix("HTTPSampler."): c.text or "" for c in sampler}
    defaults = _defaults(scope)
    path = req["path"] or "/"
    if re.match(r"^https?://", path, re.IGNORECASE):
        url = path
    else:
        protocol = prop.get("protocol") or defaults.get("protocol") or "http"
        host = prop.get("domain") or defaults.get("domain", "")
        port = prop.get("port") or defaults.get("port", "")
        if port in ("", {"http": "80", "https": "443"}.get(protocol.lower())):
            port = ""
        url = f"{protocol}://{host}{':' + port if port else ''}{path if path.startswith('/') else '/' + path}"
    form = req["args"] or None
    return {"method": req["method"], "url": url, "headers": _headers(scope), "body": req["body"], "form": form,
            "cookie_manager": _cookie_manager(scope)}


def _as_recorded(req: dict[str, Any], previews: dict[str, str]) -> dict[str, Any]:
    """The recording's side, with values correlated during the import shown as their masked recorded value.

    The import replaced them with ${variables} and the recorded value itself is not stored; its
    masked preview (the first characters and the length) is.
    """
    if not previews:
        return req

    def put(text: Any) -> Any:
        if not isinstance(text, str) or "${" not in text:
            return text
        for var, preview in previews.items():
            shown = f"[recorded {preview}]"
            text = text.replace(f"${{__urlencode(${{{var}}})}}", shown).replace(f"${{{var}}}", shown)
        return text

    return {**req, "url": put(req["url"]), "body": put(req["body"]),
            "headers": [[n, put(v)] for n, v in req["headers"]],
            "form": [[n, put(v)] for n, v in req["form"]] if req["form"] else req["form"]}


# ---- comparing ----------------------------------------------------------------------------------------

def _diff(part: str, name: str, recorded: Any, generated: Any, *, severity: str | None = None,
          message: str | None = None) -> dict[str, Any] | None:
    """One difference, or None when the two sides send the same thing."""
    if recorded == generated:
        return None
    if recorded is None:
        kind, sev = "extra", severity or "warning"
        text = message or f"{part} {name} is sent but was not in the recording"
    elif generated is None:
        kind, sev = "missing", severity or "warning"
        text = message or f"{part} {name} was in the recording but is not sent"
    elif _has_var(str(generated)):
        names = _variables_in(str(generated))
        kind, sev = "variable", "info"
        text = message or (f"{part} {name} is sent as " + ", ".join(f"${{{v}}}" for v in names) if names
                           else f"{part} {name} is generated by a JMeter function")
    else:
        kind, sev = "changed", severity or "warning"
        text = message or f"{part} {name} differs from the recording"
    return {"type": kind, "part": part, "name": name, "recorded": recorded, "generated": generated,
            "severity": sev, "message": text}


def _pairs_diff(part: str, recorded: list[tuple[str, str]], generated: list[tuple[str, str]]) -> list[dict[str, Any]]:
    out = []
    rec, gen = {}, {}
    for n, v in recorded:
        rec.setdefault(n, []).append(v)
    for n, v in generated:
        gen.setdefault(n, []).append(v)
    for name in list(dict.fromkeys([*rec, *gen])):
        a, b = rec.get(name, []), gen.get(name, [])
        for i in range(max(len(a), len(b))):
            d = _diff(part, name, a[i] if i < len(a) else None, b[i] if i < len(b) else None)
            if d:
                out.append(d)
    return out


def _json_leaves(data: Any, path: str = "$") -> dict[str, str]:
    if isinstance(data, dict):
        out: dict[str, str] = {}
        for k, v in data.items():
            out.update(_json_leaves(v, f"{path}.{k}"))
        return out
    if isinstance(data, list):
        out = {}
        for i, v in enumerate(data):
            out.update(_json_leaves(v, f"{path}[{i}]"))
        return out
    return {path: json.dumps(data) if not isinstance(data, str) else data}


_REF = re.compile(r"\$\{(?:[^{}]|\{[^{}]*\})*\}")       # ${x}, ${__urlencode(${x})}


def _json_with_variables(text: str) -> Any:
    """Parse a JSON body whose values may be ${...} references (bare ones, such as "qty": ${qty}, too)."""
    refs: list[str] = []
    out, i, in_string = [], 0, False
    while i < len(text):
        ch = text[i]
        if ch == "\\" and in_string:
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
        elif ch == "$":
            m = _REF.match(text, i)
            if m:
                refs.append(m.group(0))
                marker = f"\x00{len(refs) - 1}\x00"
                out.append(marker if in_string else f'"{marker}"')
                i = m.end()
                continue
        out.append(ch)
        i += 1
    data = json.loads("".join(out), strict=False)

    def restore(value: Any) -> Any:
        if isinstance(value, str):
            return re.sub(r"\x00(\d+)\x00", lambda m: refs[int(m.group(1))], value)
        if isinstance(value, dict):
            return {k: restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [restore(v) for v in value]
        return value

    return restore(data)


def _body_diff(recorded: dict[str, Any], generated: dict[str, Any]) -> list[dict[str, Any]]:
    if recorded["form"] is not None or generated["form"] is not None:
        return _pairs_diff("form field", recorded["form"] or [], generated["form"] or [])
    a, b = recorded["body"], generated["body"]
    if a == b:
        return []
    if a is None or b is None:
        return [d for d in [_diff("body", "", a, b)] if d]
    try:
        ja, jb = _json_with_variables(a), _json_with_variables(b)
    except ValueError:
        ja = jb = None
    if isinstance(ja, (dict, list)) and isinstance(jb, (dict, list)):
        la, lb = _json_leaves(ja), _json_leaves(jb)
        return [d for key in dict.fromkeys([*la, *lb]) if (d := _diff("JSON field", key, la.get(key), lb.get(key)))]
    if _is_form(a):
        return _pairs_diff("form field", parse_qsl(a, keep_blank_values=True), parse_qsl(b, keep_blank_values=True))
    return [d for d in [_diff("body", "", a, b)] if d]


def _is_form(text: str) -> bool:
    return "=" in text and not text.lstrip().startswith(("{", "[", "<"))


def _endpoint_diff(recorded: dict[str, Any], generated: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    if recorded["method"] != generated["method"]:
        out.append(_diff("method", "", recorded["method"], generated["method"], severity="fail",
                         message=f"The method is {generated['method']}; the recording used {recorded['method']}"))
    ra, ga = urlsplit(recorded["url"]), urlsplit(generated["url"])
    for part, x, y in (("scheme", ra.scheme, ga.scheme), ("host", ra.netloc, ga.netloc), ("path", ra.path, ga.path)):
        d = _diff(part, "", x, y, severity="fail" if part != "path" or not _has_var(y) else None)
        if d:
            out.append(d)
    out += _pairs_diff("query parameter", parse_qsl(ra.query, keep_blank_values=True),
                       parse_qsl(ga.query, keep_blank_values=True))
    return out


def _header_diff(recorded: dict[str, Any], generated: dict[str, Any]) -> list[dict[str, Any]]:
    rec = {n.lower(): (n, v) for n, v in recorded["headers"] if n.lower() not in _BROWSER_HEADERS}
    gen = {n.lower(): (n, v) for n, v in generated["headers"] if n.lower() not in _BROWSER_HEADERS}
    out = []
    for key in dict.fromkeys([*rec, *gen]):
        name = (rec.get(key) or gen.get(key))[0]
        severity = "fail" if key in _REQUIRED_HEADERS and key in rec and key not in gen else None
        d = _diff("header", name, rec[key][1] if key in rec else None, gen[key][1] if key in gen else None,
                  severity=severity)
        if d:
            out.append(d)
    return out


_SESSION_COOKIE = re.compile(r"sess|auth|token|jwt|sid|login|csrf|xsrf|remember|identity", re.IGNORECASE)


def _cookie_notes(item: dict[str, Any], generated: dict[str, Any], position: dict[int, int]) -> list[dict[str, Any]]:
    """Cookies the recording sent: the Cookie Manager can only send what an earlier kept response set.

    Session and login cookies matter (a warning); others, such as analytics cookies that the page's
    scripts set, rarely do (a note).
    """
    out = []
    for cookie in item.get("cookies") or []:
        name, source = cookie["name"], cookie.get("set_by")
        severity = "warning" if _SESSION_COOKIE.search(name) else "info"
        if not generated["cookie_manager"]:
            problem = "the plan has no HTTP Cookie Manager to send it"
        elif source is None:
            problem = ("no earlier response set it: it came from before the recording started, or the page's "
                       "scripts set it (log in while recording, or add it by hand)")
        elif source not in position:
            problem = f"it is set by #{source}, which is excluded from the plan"
        elif position[source] >= position.get(item["index"], 0):
            problem = f"it is set by #{source}, which now runs later"
        else:
            continue
        out.append({"type": "unresolved", "part": "cookie", "name": name, "recorded": "(sent)", "generated": None,
                    "severity": severity, "message": f"Cookie {name} was sent in the recording, but {problem}"})
    return out


# ---- masking -----------------------------------------------------------------------------------------

HIDDEN = "********"


def _hide(value: Any) -> Any:
    """A secret becomes ********; a ${variable} is not a secret and stays readable."""
    if not isinstance(value, str) or not value or _has_var(value):
        return value
    return HIDDEN


def _masked(req: dict[str, Any]) -> dict[str, Any]:
    out = dict(req)
    out["headers"] = [[n, _hide(v) if n.lower() in SECRET_HEADERS else v] for n, v in req["headers"]]
    if req["form"]:
        out["form"] = [[n, _hide(v) if is_secret_name(n) else v] for n, v in req["form"]]
    if req["body"]:
        out["body"] = mask_body(req["body"])
    return out


def mask_body(text: str) -> str:
    """Hide the values of password, token and similar fields in a JSON or form-encoded body."""
    try:
        data = _json_with_variables(text)
    except ValueError:
        data = None
    if isinstance(data, (dict, list)):
        def walk(v: Any, key: str = "") -> Any:
            if isinstance(v, dict):
                return {k: walk(x, k) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x, key) for x in v]
            if key and is_secret_name(key) and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                return _hide(str(v))
            return v
        return json.dumps(walk(data), ensure_ascii=False)
    if _is_form(text):
        pairs = parse_qsl(text, keep_blank_values=True)
        if pairs:
            return "&".join(f"{n}={_hide(v) if is_secret_name(n) else v}" for n, v in pairs)
    return text


def _masked_diff(d: dict[str, Any]) -> dict[str, Any]:
    secret = (d["part"] == "header" and d["name"].lower() in SECRET_HEADERS) or bool(is_secret_name(d["name"]))
    if not secret or d["part"] == "token":
        return d
    return {**d, "recorded": _hide(d["recorded"]), "generated": _hide(d["generated"])}


# ---- the report ----------------------------------------------------------------------------------------

def validate(script: dict[str, Any], view: dict[str, Any], tree: PlanTree, built: script_builder.Built, *,
             reveal: bool = False) -> dict[str, Any]:
    """Compare every kept request with the plan; see the module docstring."""
    design = ScriptDesign(**(script.get("design") or {}))
    static = set(design.static_values)
    base, _ = script_builder.build_tree({**script, "design": {}}, view)
    parents, base_parents = _parents(tree.root), _parents(base.root)
    order = [i for i in view.get("order") or range(len(script["items"])) if i in tree.samplers]
    position = {index: n for n, index in enumerate(order)}
    recorded_rank = {index: n for n, index in enumerate(sorted(order))}
    transaction_of = {i: t["name"] for t in view["transactions"] for i in t["items"]}
    imported = {c.variable: c.preview for c in design.correlations
                if c.origin == "auto" and not c.replace and c.preview and script["source"] == "har"}

    rows, unresolved_values, notes = [], [], []
    for n, index in enumerate(order):
        item = script["items"][index]
        generated = request_of(tree, index, parents)
        recorded = _as_recorded(request_of(base, index, base_parents) if index in base.samplers else generated, imported)
        diffs = _endpoint_diff(recorded, generated) + _header_diff(recorded, generated) + _body_diff(recorded, generated)
        diffs += _cookie_notes(item, generated, position)
        for var, users in built.unresolved.items():
            if index in users:
                diffs.append({"type": "unresolved", "part": "variable", "name": var, "recorded": None,
                              "generated": f"${{{var}}}", "severity": "fail",
                              "message": f"${{{var}}} is sent but nothing sets it, so the server receives the text "
                                         f"\"${{{var}}}\""})
        for var in built.empty:
            if index in built.used.get(var, []):
                diffs.append({"type": "unresolved", "part": "variable", "name": var, "recorded": None,
                              "generated": f"${{{var}}}", "severity": "warning",
                              "message": f"${{{var}}} is defined but empty (a recorded credential): give it a value"})
        for rule in design.correlations:
            if index in built.used.get(rule.variable, []):
                if rule.source not in position:
                    diffs.append({"type": "unresolved", "part": "correlation", "name": rule.variable, "recorded": None,
                                  "generated": None, "severity": "fail",
                                  "message": f"${{{rule.variable}}} is captured from #{rule.source}, which is not in the plan"})
                elif position[rule.source] >= n:
                    diffs.append({"type": "changed", "part": "order", "name": rule.variable, "recorded": None,
                                  "generated": None, "severity": "fail",
                                  "message": f"${{{rule.variable}}} is captured from #{rule.source}, which runs after this request"})
        fake_item = {"url": generated["url"], "body": generated["body"],
                     "headers": [h for h in generated["headers"] if h[0].lower() != "cookie"]}
        seen: set[str] = set()
        for value, name in list(_request_values(fake_item)) + [(v, k) for k, v in generated["form"] or []]:
            if value in seen or not looks_dynamic(value) or _has_var(value) or value in static:
                continue
            seen.add(value)
            secret = bool(is_secret_name(name))
            unresolved_values.append({"index": index, "name": name, "preview": mask(value),
                                      "value": value if reveal or not secret else None})
            diffs.append({"type": "unresolved", "part": "token", "name": name, "recorded": mask(value),
                          "generated": mask(value), "severity": "warning",
                          "message": f"{name} still sends a recorded value that looks dynamic ({mask(value)}): "
                                     "correlate it, make it a parameter, or mark it static"})
        recorded_position = recorded_rank[index]
        if recorded_position != n:
            diffs.append({"type": "changed", "part": "order", "name": "", "recorded": recorded_position + 1,
                          "generated": n + 1, "severity": "warning",
                          "message": f"Runs as request {n + 1} of the flow; it was request {recorded_position + 1} in the recording"})
        worst = max((_RANK[d["severity"]] for d in diffs), default=0)
        status = "fail" if worst == 2 else "warn" if worst == 1 else "pass"
        rows.append({
            "index": index, "label": item["label"], "transaction": transaction_of.get(index), "position": n + 1,
            "status": status,
            "recorded": recorded if reveal else _masked(recorded),
            "generated": generated if reveal else _masked(generated),
            "differences": diffs if reveal else [_masked_diff(d) for d in diffs],
        })
    for warning in built.warnings:
        notes.append(warning)
    if built.scripted:
        notes.append("The plan has JSR223 or BeanShell elements, which may set variables the validator cannot see.")

    counts = {"requests": len(rows), "pass": 0, "warn": 0, "fail": 0,
              "missing": 0, "extra": 0, "changed": 0, "unresolved": 0, "variable": 0}
    for row in rows:
        counts[row["status"]] += 1
        for d in row["differences"]:
            counts[d["type"]] += 1
    status = "fail" if counts["fail"] else "warn" if counts["warn"] else "pass"
    return {"status": status, "label": LABELS[status], "counts": counts, "requests": rows,
            "unresolved_values": unresolved_values,
            "unresolved_variables": [{"name": v, "requests": users} for v, users in sorted(built.unresolved.items())],
            "notes": notes}


def brief(report: dict[str, Any]) -> dict[str, Any]:
    """Status and counts only (stored with debug runs and shown on the script page)."""
    return {"status": report["status"], "label": report["label"], "counts": report["counts"]}


def problems(report: dict[str, Any], limit: int = 3) -> list[str]:
    """The first failing differences, for a message that says why a run cannot start."""
    out = []
    for row in report["requests"]:
        for d in row["differences"]:
            if d["severity"] == "fail":
                out.append(f"#{row['index']} {row['label']}: {d['message']}")
                if len(out) >= limit:
                    return out
    return out
