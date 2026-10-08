"""Automatic correlation for HAR recordings (requirements 8.2).

A value a request sends (query, form or JSON field, header, path segment) that looks dynamic
and also appears in an earlier response was issued by the server. The most recent earlier
response containing it becomes its source:

* JSON response with the value as a field: a JSON path, e.g. $.token;
* response headers (Location, Set-Cookie, ...): a regular expression on the headers;
* any other text: left and right boundaries taken from the text around the value, the way
  LoadRunner's web_reg_save_param works.

Every request that sends the value gets ${variable} instead (${__urlencode(${variable})} where
it was URL-encoded). Dynamic-looking values that no response contains are reported as
parameter suggestions, because the browser generated them.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit

MAX_CANDIDATES = 2000
MAX_SEARCH_CHARS = 2_000_000           # per response
SEARCH_BUDGET_CHARS = 3_000_000_000    # in total, so a huge recording cannot stall the import
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_TOKEN = re.compile(r"^[A-Za-z0-9_\-.~%+/=]+$")
_HEADER_CAPTURE = re.compile(r"^[^\s;&,\"']+$")
_AUTH_SCHEME = re.compile(r"^(Bearer|Token|Basic)\s+", re.IGNORECASE)
_FORM = re.compile(r"^[^=&\s]+=[^&]*(&[^=&\s]+=[^&]*)*$")
_JSON_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Headers whose values describe the browser, never something the server issued.
_STATIC_HEADERS = {
    "user-agent", "accept", "accept-language", "accept-encoding", "content-type", "origin", "referer",
    "cache-control", "pragma", "dnt", "upgrade-insecure-requests", "priority", "te",
}


def looks_dynamic(value: str) -> bool:
    if not 8 <= len(value) <= 4096 or not _TOKEN.match(value) or value.startswith(("http", "/")):
        return False
    if value.isdigit():
        return True
    if _UUID.match(value):
        return True
    return sum(c.isalpha() for c in value) >= 2 and sum(c.isdigit() for c in value) >= 2


def mask(value: str) -> str:
    return f"{value[:4]}… ({len(value)} characters)"


def variable_name(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")[:40] or "value"
    if base[0].isdigit():
        base = f"v_{base}"
    candidate, n = base, 2
    while candidate.lower() in taken:
        candidate, n = f"{base}_{n}", n + 1
    taken.add(candidate.lower())
    return candidate


def _json_path(parent: str, key: str) -> str:
    return f"{parent}.{key}" if _JSON_KEY.match(key) else f"{parent}['{key.replace(chr(39), chr(92) + chr(39))}']"


def walk_json(data: Any, path: str = "$", key: str = "") -> Iterator[tuple[str, str, str]]:
    """(JSON path, field name, value) for every string or integer value."""
    if isinstance(data, dict):
        for k, v in data.items():
            yield from walk_json(v, _json_path(path, str(k)), str(k))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            yield from walk_json(v, f"{path}[{i}]", key)
    elif isinstance(data, (str, int)) and not isinstance(data, bool):
        yield path, key, str(data)


def _header(item: dict[str, Any], name: str) -> str:
    return next((v for n, v in item["headers"] if n.lower() == name), "")


def _request_values(item: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """(value, name of the field that carries it) for everything a request sends."""
    parts = urlsplit(item["url"])
    for name, value in parse_qsl(parts.query):
        yield value, name
    segments = [unquote(s) for s in parts.path.split("/")]
    for i, segment in enumerate(segments):
        if segment:
            yield segment, (f"{segments[i - 1]}_id" if i and segments[i - 1] else "path_value")
    body = item.get("body")
    if body:
        ctype = _header(item, "content-type").lower()
        if "json" in ctype or body.lstrip()[:1] in ("{", "["):
            try:
                for _, key, value in walk_json(json.loads(body)):
                    yield value, key or "value"
            except ValueError:
                pass
        elif "x-www-form-urlencoded" in ctype or _FORM.match(body.strip()):
            for name, value in parse_qsl(body):
                yield value, name
    for name, value in item["headers"]:
        low = name.lower()
        if low in _STATIC_HEADERS or "${" in value:
            continue
        scheme = _AUTH_SCHEME.match(value)
        yield (value[scheme.end():], "token") if scheme else (value, low.replace("-", "_"))


def _headers_text(headers: list[list[str]]) -> str:
    return "\n".join(f"{n}: {v}" for n, v in headers)


def _boundaries(text: str, pos: int, value: str) -> tuple[str, str] | None:
    """Left/right boundaries around text[pos:pos+len(value)] whose first match is this value.

    JMeter's Boundary Extractor takes the first left boundary, then the text up to the next
    right boundary; the left boundary is widened until that first match is our value.
    """
    end = pos + len(value)
    right = text[end:end + 8].split("\n")[0] or ("\n" if text[end:end + 1] == "\n" else "")
    if not right:
        return None
    for width in (12, 20, 32, 48, 64):
        left = text[max(0, pos - width):pos]
        if "\n" in left:
            left = left[left.rindex("\n") + 1:]       # stay on the value's line
        if len(left) < 3:
            return None
        start = text.find(left) + len(left)
        stop = text.find(right, start)
        if stop >= 0 and text[start:stop] == value:
            return left, right
    return None


def _extractor(value: str, body: str | None, mime: str, headers: list[list[str]]) -> dict[str, Any] | None:
    if body and value in body:
        if "json" in mime or body.lstrip()[:1] in ("{", "["):
            try:
                paths = [(p, k) for p, k, v in walk_json(json.loads(body)) if v == value]
            except ValueError:
                paths = []
            if paths:
                path, key = min(paths, key=lambda pk: len(pk[0]))
                return {"extractor": "json", "expression": path, "right": None, "scope": "body", "field": key}
        bounds = _boundaries(body, body.index(value), value)
        if bounds:
            return {"extractor": "boundary", "expression": bounds[0], "right": bounds[1], "scope": "body", "field": None}
    for name, header_value in headers:
        if value in header_value and _HEADER_CAPTURE.match(value) and name.lower() != "content-length":
            prefix = header_value[:header_value.index(value)][-24:]
            field = re.search(r"([A-Za-z0-9_\-]+)=$", prefix)
            return {"extractor": "regex", "expression": f"(?i){re.escape(name)}: [^\\n]*?{re.escape(prefix)}([^\\s;&,\"']+)",
                    "right": None, "scope": "headers", "field": field.group(1) if field else name}
    return None


def replace_value(text: str, value: str, variable: str) -> tuple[str, int]:
    """Replace whole occurrences of value (raw, JSON-escaped or URL-encoded) with the variable."""
    total = 0
    forms = [(value, f"${{{variable}}}")]
    if "/" in value:
        forms.append((value.replace("/", "\\/"), f"${{{variable}}}"))
    for encoded in dict.fromkeys([quote(value, safe=""), quote_plus(value, safe="")]):
        if encoded != value:
            forms.append((encoded, f"${{__urlencode(${{{variable}}})}}"))
    for found, replacement in forms:
        if found not in text:
            continue
        # A whole value: not part of a longer token, but it may follow a %XX escape (encoded URLs).
        pattern = re.compile(rf"(?:(?<=%[0-9A-Fa-f]{{2}})|(?<![A-Za-z0-9])){re.escape(found)}(?![A-Za-z0-9])")
        text, n = pattern.subn(lambda _: replacement, text)
        total += n
    return text, total


def _apply(item: dict[str, Any], value: str, variable: str) -> int:
    count = 0
    item["url"], n = replace_value(item["url"], value, variable)
    count += n
    if item.get("body"):
        item["body"], n = replace_value(item["body"], value, variable)
        count += n
    for header in item["headers"]:
        header[1], n = replace_value(header[1], value, variable)
        count += n
    return count


def detect(items: list[dict[str, Any]], responses: list[dict[str, Any]]) -> dict[str, Any]:
    """Correlate in place. `responses[i]`: {"body": text or None, "mime": str, "headers": [[n, v]]}.

    Returns {"correlations": [...], "client_values": [...]} (client values: dynamic values no
    response contained, offered as parameter suggestions).
    """
    first_use: dict[str, tuple[int, str]] = {}
    for index, item in enumerate(items):
        for value, name in _request_values(item):
            if value not in first_use and looks_dynamic(value) and "${" not in value:
                first_use[value] = (index, name)
                if len(first_use) >= MAX_CANDIDATES:
                    break

    taken: set[str] = set()
    correlations: list[dict[str, Any]] = []
    client_values: list[dict[str, Any]] = []
    budget = SEARCH_BUDGET_CHARS
    # Longest first, so a value that contains another is replaced whole.
    for value, (use, name) in sorted(first_use.items(), key=lambda kv: (-len(kv[0]), kv[1][0])):
        if value not in _request_text(items[use]):
            continue                    # already replaced as part of a longer value
        found = None
        for source in range(use - 1, -1, -1):
            resp = responses[source]
            body = resp["body"][:MAX_SEARCH_CHARS] if resp["body"] else None
            budget -= len(body or "")
            found = _extractor(value, body, resp["mime"], resp["headers"])
            if found or budget < 0:
                break
        if budget < 0:
            return {"correlations": correlations, "client_values": client_values, "stopped_early": True}
        if not found:
            kind = _client_kind(value, items[use].get("started_ms"))
            # UUIDs and timestamps are not secrets: keep them so one click can parameterize them.
            client_values.append({"name": name, "preview": mask(value), "first_used": use, "kind": kind,
                                  "value": value if kind in ("uuid", "timestamp") else None})
            continue
        variable = variable_name(found.pop("field") or name, taken)
        used_in = [i for i in range(use, len(items)) if _apply(items[i], value, variable)]
        correlations.append({
            "id": f"c{len(correlations) + 1}", "variable": variable, "source": source, **found,
            "match": 1, "replace": None, "origin": "auto", "preview": mask(value), "used_in": used_in,
        })
    correlations.sort(key=lambda c: (c["source"], c["variable"]))
    for n, rule in enumerate(correlations, 1):
        rule["id"] = f"c{n}"
    return {"correlations": correlations, "client_values": client_values, "stopped_early": False}


def _request_text(item: dict[str, Any]) -> str:
    return "\n".join([item["url"], item.get("body") or "", *(v for _, v in item["headers"])])


def _client_kind(value: str, started_ms: int | None) -> str:
    if _UUID.match(value):
        return "uuid"
    if value.isdigit() and started_ms and len(value) in (10, 13):
        stamp = int(value) * (1000 if len(value) == 10 else 1)
        if abs(stamp - started_ms) < 86_400_000:
            return "timestamp"
    return "value"


def suggest_checks(body: str | None, mime: str) -> dict[str, Any] | None:
    """A stable text or JSON check from a recorded response (requirements 8.6)."""
    if not body:
        return None
    if "json" in mime:
        try:
            data = json.loads(body)
        except ValueError:
            return None
        if isinstance(data, dict):
            for key in ("success", "ok"):
                if data.get(key) is True:
                    return {"type": "json_path", "path": f"$.{key}", "expected": "true"}
            status = data.get("status")
            if isinstance(status, str) and status.lower() in {"ok", "success", "succeeded", "active"}:
                return {"type": "json_path", "path": "$.status", "expected": status}
            keys = [k for k in data if _JSON_KEY.match(str(k))]
            if keys:
                return {"type": "json_path", "path": f"$.{keys[0]}", "expected": None}
        return None
    if "html" in mime:
        title = re.search(r"<title[^>]*>([^<]{3,80})</title>", body, re.IGNORECASE)
        if title and title.group(1).strip():
            return {"type": "text", "value": title.group(1).strip()}
    return None
