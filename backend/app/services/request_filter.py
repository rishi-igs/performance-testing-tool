"""Request filtering (requirements 8.3) and grouping into business functions.

For every request the tool decides:
  include      your manual choice, otherwise "not matched by an exclusion rule"
  transaction  your manual name, otherwise the first matching transaction rule, otherwise
               an automatic name (HAR: a new group on each page navigation or idle gap;
               JMX: the controller the sampler was in)
Kept requests that follow each other with the same name form one Transaction Controller.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from ..models.script import FilterRules, TransactionRule

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,}$")
_VERSION = re.compile(r"^v\d+(\.\d+)*$", re.IGNORECASE)
_PAGE_EXTENSION = re.compile(r"\.(html?|php|aspx?|jsp|do|action|cgi)$", re.IGNORECASE)
_GENERIC_SEGMENTS = {"api", "rest", "services", "service", "public", "web", "app"}

PUBLIC_FIELDS = ("index", "kind", "label", "method", "url", "host", "status", "mime", "resource_type",
                 "size", "time_ms", "note")


def _looks_like_id(segment: str) -> bool:
    return (segment.isdigit() or bool(_UUID.match(segment))
            or (bool(_TOKEN.match(segment)) and any(c.isdigit() for c in segment)))


def name_from_url(url: str | None) -> str:
    """Readable business-function name from a URL path: /api/v1/auth/login -> "Auth Login"."""
    words = []
    for segment in urlsplit(url or "").path.split("/"):
        segment = _PAGE_EXTENSION.sub("", unquote(segment)).strip()
        if (not segment or _looks_like_id(segment) or _VERSION.match(segment)
                or segment.lower() in _GENERIC_SEGMENTS):
            continue
        words.append(re.sub(r"[-_.+]+", " ", segment))
    text = " ".join(words[-2:]).split()
    return " ".join(w[:1].upper() + w[1:] for w in text)[:80] or "Home"


def _domain_matches(host: str, pattern: str) -> bool:
    return (pattern.startswith("*.") and host == pattern[2:]) or fnmatch.fnmatchcase(host, pattern)


def _is_api(item: dict[str, Any]) -> bool | None:
    """True for XHR/fetch, JSON/XML responses and form posts; None when the recording does not say."""
    resource_type = (item.get("resource_type") or "").lower()
    mime = item.get("mime") or ""
    if not resource_type and not mime:
        return None
    if resource_type in {"xhr", "fetch"} or "json" in mime or (mime.endswith("xml") and "html" not in mime):
        return True
    return item.get("method") not in {None, "GET", "HEAD"}


def auto_reason(item: dict[str, Any], rules: FilterRules) -> str | None:
    """Why the rules exclude this request, or None if they keep it."""
    if item.get("disabled"):
        return "Disabled in the imported plan"
    if item["kind"] != "http":
        return None
    url = item.get("url") or ""
    if not url.lower().startswith(("http://", "https://")) and "${" not in url:
        return "Not an HTTP request (browser-internal)"
    if item.get("error"):
        return f"Cancelled or failed in the browser ({item['error']})"
    method = item.get("method") or "GET"
    if method in rules.exclude_methods:
        return "Browser preflight (OPTIONS)" if method == "OPTIONS" else f"{method} requests are excluded"
    host = item.get("host") or ""
    pattern = next((p for p in rules.exclude_domains if _domain_matches(host, p)), None)
    if pattern:
        return f"Third-party domain ({pattern})"
    last = urlsplit(url).path.rsplit("/", 1)[-1]
    extension = last.rsplit(".", 1)[1].lower() if "." in last else ""
    if extension and extension in rules.exclude_extensions:
        return f"Static file (.{extension})"
    mime = item.get("mime") or ""
    if mime and any(fnmatch.fnmatchcase(mime, p) for p in rules.exclude_mime_types):
        return f"Static content ({mime})"
    if rules.keep_only_api and _is_api(item) is False:
        return "Not an API call"
    return None


def _rule_name(item: dict[str, Any], rules: list[TransactionRule]) -> str | None:
    url = item.get("url")
    target = (url.split("?", 1)[0].split("#", 1)[0] if url else item.get("label") or "").lower()
    return next((r.name for r in rules if fnmatch.fnmatchcase(target, r.url_pattern.lower())), None)


def _har_names(items: list[dict[str, Any]], reasons: list[str | None], gap_seconds: float) -> list[str]:
    """Split the recording at page navigations and idle gaps; name each part after its first API call or page."""
    segments: list[int] = []
    segment, previous_page, previous_end = -1, None, None
    for n, (item, reason) in enumerate(zip(items, reasons)):
        start = item.get("started_ms")
        idle = (gap_seconds > 0 and reason is None and start is not None and previous_end is not None
                and start - previous_end >= gap_seconds * 1000)
        if n == 0 or item.get("page") != previous_page or idle:
            segment += 1
        segments.append(segment)
        previous_page = item.get("page")
        if reason is None and start is not None:
            previous_end = start + (item.get("time_ms") or 0)

    members: dict[int, list[int]] = {}
    for n, seg in enumerate(segments):
        members.setdefault(seg, []).append(n)
    names: dict[int, str] = {}
    pages_named: set[str] = set()
    for seg, positions in members.items():
        kept = [n for n in positions if reasons[n] is None and items[n]["kind"] == "http"]
        lead = items[(kept or positions)[0]]
        page = lead.get("page")
        if lead.get("page_name") and page not in pages_named:
            names[seg] = lead["page_name"]
        else:
            names[seg] = name_from_url(lead.get("url"))
        if page:
            pages_named.add(page)
    return [names[s] for s in segments]


def _jmx_names(items: list[dict[str, Any]]) -> list[str]:
    """The controller a sampler was in; otherwise a name from its URL."""
    names: list[str] = []
    for item in items:
        if item.get("page_name"):
            names.append(item["page_name"])
        elif item["kind"] == "http":
            names.append(name_from_url(item.get("url")))
        else:
            names.append(names[-1] if names else item["label"][:80])
    return names


def evaluate(source: str, items: list[dict[str, Any]], rules: FilterRules,
             overrides: dict[str, dict[str, Any]]) -> dict[str, Any]:
    reasons = [auto_reason(item, rules) for item in items]
    auto_names = _har_names(items, reasons, rules.idle_gap_seconds) if source == "har" else _jmx_names(items)

    views: list[dict[str, Any]] = []
    for item, reason, auto_name in zip(items, reasons, auto_names):
        edit = overrides.get(str(item["index"]), {})
        rule = _rule_name(item, rules.transaction_rules)
        if "transaction" in edit:
            name, name_source = edit["transaction"], "manual"
        elif rule:
            name, name_source = rule, "rule"
        else:
            name, name_source = auto_name, "auto"
        views.append({
            **{k: item.get(k) for k in PUBLIC_FIELDS},
            "has_body": item.get("body") is not None,
            "include": edit["include"] if "include" in edit else reason is None,
            "include_source": "manual" if "include" in edit else "auto",
            "auto_reason": reason,
            "transaction": name,
            "transaction_source": name_source,
            "transaction_number": None,
        })

    transactions: list[dict[str, Any]] = []
    previous = None
    for item, view in zip(items, views):
        if not view["include"]:
            continue
        key = (item.get("thread_group", 0), view["transaction"])
        if key != previous:
            transactions.append({"number": len(transactions) + 1, "name": view["transaction"],
                                 "thread_group": key[0], "items": []})
            previous = key
        transactions[-1]["items"].append(item["index"])
        view["transaction_number"] = transactions[-1]["number"]

    kept = sum(1 for v in views if v["include"])
    return {
        "items": views,
        "transactions": transactions,
        "summary": {"captured": len(items), "kept": kept, "excluded": len(items) - kept,
                    "transactions": len(transactions)},
    }
