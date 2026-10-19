"""Detect HAR response values that are replayed by later requests."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, quote_plus

_DYNAMIC_NAME = re.compile(r"token|session|csrf|auth|nonce|uuid|id$", re.IGNORECASE)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VARIABLE = re.compile(r"[^A-Za-z0-9_]+")


def _flatten(value: Any, path: str = "$", key: str = "") -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    if isinstance(value, dict):
        for name, child in value.items():
            child_path = f"{path}.{name}" if _IDENTIFIER.fullmatch(name) else f"{path}['{name}']"
            found.extend(_flatten(child, child_path, name))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_flatten(child, f"{path}[{index}]", key))
    elif value is not None and not isinstance(value, bool):
        text = str(value)
        if len(text) >= 5:
            found.append((text, path, key))
    return found


def _json_response(item: dict[str, Any]) -> Any:
    text = item.get("_response_text")
    if not isinstance(text, str) or not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _value_variants(value: str) -> list[str]:
    return sorted({value, quote(value, safe=""), quote_plus(value, safe="")}, key=len, reverse=True)


def _replace_request_value(item: dict[str, Any], variants: list[str], variable: str,
                           raw_headers: dict[str, str]) -> None:
    replacement = "${" + variable + "}"
    for field in ("url", "body"):
        if isinstance(item.get(field), str):
            for value in variants:
                item[field] = item[field].replace(value, replacement)
    raw_by_name = {name.lower(): header_value for name, header_value in raw_headers.items()}
    sanitized_headers = []
    for name, header_value in item.get("headers", []):
        raw_value = raw_by_name.get(name.lower(), header_value)
        for value in variants:
            raw_value = raw_value.replace(value, replacement)
        sanitized_headers.append([name, raw_value])
    item["headers"] = sanitized_headers


def detect_correlations(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return extractor rules and remove recorded dynamic values from request data."""
    rules: list[dict[str, Any]] = []
    warnings: list[str] = []
    used_names: set[str] = set()
    used_values: set[str] = set()

    for source_position, source in enumerate(items):
        payload = _json_response(source)
        if payload is None:
            continue
        for value, expression, key in _flatten(payload):
            if value in used_values or (len(value) < 12 and not _DYNAMIC_NAME.search(key)):
                continue
            variants = _value_variants(value)
            targets: list[int] = []
            for target in items[source_position + 1:]:
                request_parts = [
                    target.get("url") or "",
                    target.get("body") or "",
                    *(header_value for _, header_value in target.get("headers", [])),
                ]
                raw_request = target.get("_raw_request", {})
                request_parts.extend([
                    str(raw_request.get("url") or ""),
                    str(raw_request.get("body") or ""),
                    *(str(v) for v in raw_request.get("headers", {}).values()),
                ])
                if any(variant in part for part in request_parts for variant in variants):
                    targets.append(target["index"])
            if not targets:
                continue

            base = _VARIABLE.sub("_", key).strip("_") or "correlated_value"
            if not base[0].isalpha() and base[0] != "_":
                base = "correlated_" + base
            name = base[:64]
            suffix = 2
            while name in used_names:
                tail = f"_{suffix}"
                name = base[:64 - len(tail)] + tail
                suffix += 1
            used_names.add(name)
            used_values.add(value)
            rules.append({
                "source_index": source["index"],
                "target_indices": targets,
                "variable": name,
                "extractor": "json",
                "expression": expression,
                "enabled": True,
            })
            for target in items[source_position + 1:]:
                if target["index"] not in targets:
                    continue
                _replace_request_value(
                    target, variants, name, target.get("_raw_request", {}).get("headers", {}),
                )

    if rules:
        warnings.append(
            f"Detected {len(rules)} response value(s) reused by later requests. Review the correlation "
            "rules and verify the generated JMeter extractors against your target application."
        )
    for item in items:
        item.pop("_response_text", None)
        item.pop("_raw_request", None)
    return rules, warnings
