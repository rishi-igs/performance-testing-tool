"""Validate uploaded parameter CSV metadata and replace recorded request values."""
from __future__ import annotations

import csv
import io
import re
from typing import Any

_COLUMN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def inspect_csv(raw: bytes) -> tuple[list[str], int]:
    try:
        text = raw.decode("utf-8-sig")
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        headers = next(rows)
        if not headers or any(not _COLUMN_NAME.fullmatch(name) for name in headers):
            raise ValueError("CSV headers must be non-empty JMeter variable names.")
        if len(headers) != len(set(headers)):
            raise ValueError("CSV column names must be unique.")
        row_count = 0
        for row_count, row in enumerate(rows, start=1):
            if len(row) != len(headers):
                raise ValueError(f"CSV row {row_count + 1} has {len(row)} columns; expected {len(headers)}.")
        if row_count == 0:
            raise ValueError("CSV must contain a header and at least one data row.")
        return headers, row_count
    except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
        raise ValueError(f"CSV must be valid UTF-8 with a header and consistent rows: {exc}") from exc


def apply_parameters(
    items: list[dict[str, Any]],
    previous: dict[str, Any],
    parameters: list[dict[str, Any]],
    headers: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if parameters and not headers:
        raise ValueError("Upload a CSV file before configuring request parameters.")
    for rule in parameters:
        if rule["column"] not in headers:
            raise ValueError(f"CSV does not contain the column '{rule['column']}'.")

    old_by_column = {rule["column"]: rule for rule in previous.get("parameters", [])}
    changes = []
    for old in previous.get("parameters", []):
        replacement = next((rule for rule in parameters if rule["column"] == old["column"]), None)
        if replacement is None:
            placeholder = "${" + old["variable"] + "}"
            if any(placeholder in _request_text(item) for item in items):
                raise ValueError(f"Parameter ${{{old['variable']}}} is already used in a request and cannot be removed.")
        elif replacement["variable"] != old["variable"]:
            changes.append((old["variable"], replacement["variable"]))

    updated = [dict(item) for item in items]
    for item in updated:
        for field in ("url", "body"):
            value = item.get(field)
            if isinstance(value, str):
                for old_name, new_name in changes:
                    value = value.replace("${" + old_name + "}", "${" + new_name + "}")
                item[field] = value
        headers_out = []
        for name, value in item.get("headers", []):
            for old_name, new_name in changes:
                value = value.replace("${" + old_name + "}", "${" + new_name + "}")
            headers_out.append([name, value])
        item["headers"] = headers_out

    seen_values: set[str] = set()
    stored = []
    for rule in parameters:
        old = old_by_column.get(rule["column"])
        recorded_value = rule.get("recorded_value")
        if old is None:
            if recorded_value is None:
                raise ValueError(
                    f"Parameter '{rule['variable']}' needs a recorded_value the first time it is added."
                )
        if recorded_value is not None:
            if "${" in recorded_value:
                raise ValueError("recorded_value must be a literal value, not a JMeter variable.")
            if recorded_value in seen_values:
                raise ValueError("Each parameter must use a distinct recorded_value.")
            seen_values.add(recorded_value)
            replacement = "${" + rule["variable"] + "}"
            count = 0
            for item in updated:
                for field in ("url", "body"):
                    value = item.get(field)
                    if isinstance(value, str):
                        count += value.count(recorded_value)
                        item[field] = value.replace(recorded_value, replacement)
                headers_out = []
                for name, value in item.get("headers", []):
                    count += value.count(recorded_value)
                    headers_out.append([name, value.replace(recorded_value, replacement)])
                item["headers"] = headers_out
            if not count:
                raise ValueError(f"Recorded value for '{rule['variable']}' was not found in any request.")
        else:
            count = old["replacements"]
        stored.append({"variable": rule["variable"], "column": rule["column"], "replacements": count})

    return updated, {
        "parameters": stored,
        "recycle": bool(previous.get("recycle", True)),
        "sharing_mode": previous.get("sharing_mode", "all_threads"),
    }


def _request_text(item: dict[str, Any]) -> str:
    return " ".join([
        item.get("url") or "",
        item.get("body") or "",
        *(value for _, value in item.get("headers", [])),
    ])
