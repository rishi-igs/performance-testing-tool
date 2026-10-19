"""Parse a browser HAR recording into a sanitized list of requests.

Only what is needed to rebuild the requests is kept. Response bodies are read during import
to find where dynamic values come from (correlation) and to suggest checks, then dropped.
Cookies are removed (JMeter's Cookie Manager handles them during a run) and credential
headers that no response issued are replaced with JMeter variables, so recorded secrets are
not stored.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from ..models.test_config import SENSITIVE_HEADERS
from . import correlation

MAX_REQUESTS = 10_000
MAX_BODY_CHARS = 1_000_000
_TEXT_MIME = re.compile(r"json|xml|html|text/plain|x-www-form-urlencoded", re.IGNORECASE)

# Set by the browser or by JMeter itself; replaying the recorded value would be wrong.
# Host, If-Modified-Since and If-None-Match match JMeter's recorder default (proxy.headers.remove).
DROP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "proxy-connection", "transfer-encoding",
    "if-modified-since", "if-none-match", "cookie",
}
SECRET_HEADERS = (SENSITIVE_HEADERS - {"cookie"}) | {"x-csrf-token", "x-xsrf-token"}
_AUTH_SCHEME = re.compile(r"^(Bearer|Basic|Token|Digest)\s+", re.IGNORECASE)


class ImportFailed(ValueError):
    """The upload could not be read; the message is shown to the user."""


def _epoch_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


def _clean_headers(raw: Any, dropped: set[str]) -> list[list[str]]:
    headers: list[list[str]] = []
    for h in raw if isinstance(raw, list) else []:
        if not isinstance(h, dict):
            continue
        name, value = str(h.get("name") or "").strip(), str(h.get("value") or "")
        low = name.lower()
        if not name or name.startswith(":"):          # HTTP/2 pseudo-headers
            continue
        if low in DROP_HEADERS:
            dropped.add(low)
            continue
        if any(c in name for c in "\r\n:") or any(c in value for c in "\r\n"):
            continue
        headers.append([name, value])
    return headers


def _hide_secrets(items: list[dict[str, Any]]) -> set[str]:
    """Credential headers still holding a recorded value get a ${variable} instead."""
    secrets: set[str] = set()
    for item in items:
        for header in item["headers"]:
            low = header[0].lower()
            if low in SECRET_HEADERS and "${" not in header[1]:
                var = low.replace("-", "_")
                scheme = _AUTH_SCHEME.match(header[1])
                header[1] = f"{scheme.group(0) if scheme else ''}${{{var}}}"
                secrets.add(var)
    return secrets


def _response(resp: dict[str, Any], mime: str) -> dict[str, Any]:
    """What correlation needs from a response: text body (if textual), mime and headers."""
    content = resp.get("content") if isinstance(resp.get("content"), dict) else {}
    text = content.get("text") if isinstance(content.get("text"), str) else None
    if text is not None and content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text, validate=False).decode("utf-8", errors="replace") if _TEXT_MIME.search(mime) else None
        except (binascii.Error, ValueError):
            text = None
    if text is not None and not _TEXT_MIME.search(mime) and not text.lstrip()[:1] in ("{", "[", "<"):
        text = None
    headers = [[str(h.get("name", "")), str(h.get("value", ""))]
               for h in resp.get("headers") or [] if isinstance(h, dict)]
    return {"body": text[:correlation.MAX_SEARCH_CHARS] if text else None, "mime": mime, "headers": headers}


def _body(post: Any) -> tuple[str | None, str | None]:
    """Return (body text, mime type) from a HAR postData object."""
    if not isinstance(post, dict):
        return None, None
    text = post.get("text")
    params = post.get("params")
    if not isinstance(text, str) and isinstance(params, list) and params:
        text = urlencode([(str(p.get("name", "")), str(p.get("value", ""))) for p in params if isinstance(p, dict)])
    mime = post.get("mimeType") if isinstance(post.get("mimeType"), str) else None
    return (text if isinstance(text, str) else None), mime


def _page_name(title: Any) -> str | None:
    """Chrome stores the page URL as the title; only a real document title is useful as a name."""
    if not isinstance(title, str) or not title.strip() or "://" in title:
        return None
    return title.strip()[:80]


def parse_har(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportFailed(f"This is not a HAR file: it could not be read as JSON ({exc}).") from exc
    log = data.get("log") if isinstance(data, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        raise ImportFailed("This is not a HAR file: it has no log.entries list.")
    if not entries:
        raise ImportFailed("The HAR file contains no requests.")
    if len(entries) > MAX_REQUESTS:
        raise ImportFailed(f"The HAR file has {len(entries)} requests; the limit is {MAX_REQUESTS}. Record a shorter flow.")

    pages = {
        p["id"]: _page_name(p.get("title"))
        for p in (log.get("pages") or []) if isinstance(p, dict) and isinstance(p.get("id"), str)
    }
    dropped: set[str] = set()
    truncated = multipart = 0
    items: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []

    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("request"), dict):
            continue
        req = entry["request"]
        resp = entry.get("response") if isinstance(entry.get("response"), dict) else {}
        url = str(req.get("url") or "")
        method = str(req.get("method") or "GET").upper()
        headers = _clean_headers(req.get("headers"), dropped)
        body, body_mime = _body(req.get("postData"))
        if body is not None and len(body) > MAX_BODY_CHARS:
            body, truncated = None, truncated + 1
        if body is not None and body_mime and not any(h[0].lower() == "content-type" for h in headers):
            headers.append(["Content-Type", body_mime])
        if body is not None and "multipart/form-data" in (body_mime or "").lower():
            multipart += 1

        content = resp.get("content") if isinstance(resp.get("content"), dict) else {}
        status = resp.get("status") if isinstance(resp.get("status"), int) else 0
        error = resp.get("_error") if isinstance(resp.get("_error"), str) and resp.get("_error") else None
        size = _number(content.get("size"))
        if size is None:
            size = _number(resp.get("bodySize"))
        page = entry.get("pageref") if isinstance(entry.get("pageref"), str) else None
        mime = (content.get("mimeType") or "").split(";")[0].strip().lower() if isinstance(content.get("mimeType"), str) else ""
        responses.append(_response(resp, mime))

        items.append({
            "kind": "http",
            "label": f"{method} {urlsplit(url).path or '/'}",
            "method": method,
            "url": url,
            "host": (urlsplit(url).hostname or "").lower(),
            "status": status or None,
            "mime": mime or None,
            "resource_type": entry.get("_resourceType") if isinstance(entry.get("_resourceType"), str) else None,
            "size": int(size) if size is not None else None,
            "time_ms": _number(entry.get("time")),
            "started_ms": _epoch_ms(entry.get("startedDateTime")),
            "page": page,
            "page_name": pages.get(page) if page else None,
            "error": error or (None if status else "No response recorded (cancelled or blocked)"),
            "headers": headers,
            "body": body,
        })

    if not items:
        raise ImportFailed("The HAR file contains no readable requests.")
    # Keep the recorded order; sort by start time only when every entry has one.
    if all(i["started_ms"] is not None for i in items):
        order = sorted(range(len(items)), key=lambda n: items[n]["started_ms"])
        items, responses = [items[n] for n in order], [responses[n] for n in order]
    for index, item in enumerate(items):
        item["index"] = index

    found = correlation.detect(items, responses)
    secrets = _hide_secrets(items)
    check_suggestions = [
        {"item": i, **s} for i, r in enumerate(responses)
        if (s := correlation.suggest_checks(r["body"], r["mime"])) and not items[i].get("error")
    ]

    warnings = []
    if found["correlations"]:
        warnings.append(f"{len(found['correlations'])} dynamic value(s) were correlated automatically "
                        "(see Correlation). Check them with a debug replay.")
    if found["stopped_early"]:
        warnings.append("The recording is very large, so automatic correlation stopped early. "
                        "Add the remaining correlations by hand.")
    if secrets:
        names = ", ".join(f"${{{v}}}" for v in sorted(secrets))
        warnings.append(
            f"Recorded credential headers that no response issued were replaced with variables ({names}). "
            "Give them values under Parameters, or in User Defined Variables in JMeter."
        )
    if "cookie" in dropped:
        warnings.append("Recorded cookies were removed. The HTTP Cookie Manager stores the cookies the "
                        "server sets during the run.")
    bodies = sum(1 for i in items if i["body"] is not None)
    if bodies:
        warnings.append(f"{bodies} request(s) send a recorded body. Bodies are replayed as recorded and may "
                        "contain test credentials or IDs that need parameterizing.")
    if multipart:
        warnings.append(f"{multipart} multipart upload(s) are replayed from the recorded text; binary file "
                        "content is usually missing from HAR files.")
    if truncated:
        warnings.append(f"{truncated} request body/bodies over {MAX_BODY_CHARS:,} characters were dropped.")

    title = next((n for n in pages.values() if n), None)
    first_host = next((i["host"] for i in items if i["host"]), "recording")
    return {
        "name": title or first_host, "items": items, "warnings": warnings, "variables": sorted(secrets),
        "design": {"correlations": found["correlations"]},
        "suggestions": {"checks": check_suggestions, "parameters": found["client_values"]},
    }
