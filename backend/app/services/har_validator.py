"""Compare a reviewed HAR flow with the JMeter plan generated from it."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .jmeter_plan_builder import build_recorded_plan

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_WHOLE_VARIABLE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_DYNAMIC_NAME = re.compile(r"(?:token|session|csrf|nonce|auth|(?:^|[_-])id)$", re.IGNORECASE)
_PATH_ID = re.compile(r"^(?:\d{5,}|[0-9a-f]{8}-[0-9a-f-]{27,})$", re.IGNORECASE)
_PATH_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,}$")


def _prop(element: ET.Element, name: str) -> str:
    found = element.find(f"./stringProp[@name='{name}']")
    return found.text or "" if found is not None else ""


def _port(parts: Any) -> int:
    try:
        return parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        return 443 if parts.scheme.lower() == "https" else 80


def _endpoint_key(url: str) -> tuple[str, str, int, str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), _port(parts), parts.path or "/", parts.query


def _safe_endpoint(url: str) -> str:
    parts = urlsplit(url)
    path = "/".join(
        "<redacted>" if _PATH_ID.fullmatch(segment)
        or (_PATH_TOKEN.fullmatch(segment) and any(c.isdigit() for c in segment))
        else segment
        for segment in parts.path.split("/")
    )
    query = "&".join(f"{key}=<redacted>" for key, _ in parse_qsl(parts.query, keep_blank_values=True))
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        if parts.port:
            host = f"{host}:{parts.port}"
    except ValueError:
        pass
    return urlunsplit((parts.scheme, host, path, query, ""))


def _header_pairs(headers: list[list[str]]) -> list[tuple[str, str]]:
    return sorted((name.strip().lower(), value) for name, value in headers)


def _read_plan(xml: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml)
    parents = {child: parent for parent in root.iter() for child in parent}
    sampler_trees: dict[ET.Element, ET.Element] = {}
    sampler_children: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        children = list(parent)
        for index, element in enumerate(children[:-1]):
            if (element.tag == "HTTPSamplerProxy" and children[index + 1].tag == "hashTree"):
                sampler_trees[children[index + 1]] = element
                sampler_children[element] = children[index + 1]

    global_headers: list[list[str]] = []
    local_headers: dict[ET.Element, list[list[str]]] = {}
    for manager in root.iter("HeaderManager"):
        headers = [
            [_prop(header, "Header.name"), _prop(header, "Header.value")]
            for header in manager.findall("./collectionProp[@name='HeaderManager.headers']/elementProp")
        ]
        ancestor = parents.get(manager)
        sampler = None
        while ancestor is not None:
            if ancestor in sampler_trees:
                sampler = sampler_trees[ancestor]
                break
            ancestor = parents.get(ancestor)
        if sampler is None:
            global_headers.extend(headers)
        else:
            local_headers.setdefault(sampler, []).extend(headers)

    generated = []
    for sampler in root.iter("HTTPSamplerProxy"):
        scheme = _prop(sampler, "HTTPSampler.protocol") or "http"
        host = _prop(sampler, "HTTPSampler.domain")
        port = _prop(sampler, "HTTPSampler.port")
        path = _prop(sampler, "HTTPSampler.path") or "/"
        authority = host if not port or int(port) == (443 if scheme == "https" else 80) else f"{host}:{port}"
        url = urlunsplit((scheme, authority, path.split("?", 1)[0] or "/", path.partition("?")[2], ""))
        body_arg = sampler.find(".//stringProp[@name='Argument.value']")
        body = body_arg.text or "" if body_arg is not None else None
        tree = sampler_children.get(sampler)
        assertions = []
        if tree is not None:
            assertions = [
                value.text or ""
                for value in tree.findall(".//collectionProp[@name='Asserion.test_strings']/stringProp")
            ]
        generated.append({
            "label": sampler.get("testname") or "",
            "url": url,
            "method": _prop(sampler, "HTTPSampler.method").upper(),
            "headers": global_headers + local_headers.get(sampler, []),
            "body": body,
            "status_codes": assertions,
        })
    return generated


def _dynamic_candidates(item: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    url = item.get("url") or ""
    parts = urlsplit(url)
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _DYNAMIC_NAME.search(key) and not _WHOLE_VARIABLE.fullmatch(value):
            candidates.append(f"query.{key}")
    for number, segment in enumerate(parts.path.split("/")):
        if _PATH_ID.fullmatch(segment):
            candidates.append(f"path segment {number}")

    def inspect_json(value: Any, prefix: str = "$") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}"
                if (_DYNAMIC_NAME.search(key) and isinstance(child, str)
                        and child and not _WHOLE_VARIABLE.fullmatch(child)):
                    candidates.append(path)
                else:
                    inspect_json(child, path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect_json(child, f"{prefix}[{index}]")

    body = item.get("body")
    if isinstance(body, str):
        try:
            inspect_json(json.loads(body))
        except (json.JSONDecodeError, TypeError):
            pass
    for name, value in item.get("headers", []):
        if _DYNAMIC_NAME.search(name) and value and not _VARIABLE.search(value):
            candidates.append(f"header.{name}")
    return candidates


def validate_har(
    *,
    name: str,
    items: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    correlations: list[dict[str, Any]],
    variables: list[str],
    parameterization: dict[str, Any],
    csv_headers: list[str],
    assertions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check generated request fidelity and report likely unresolved dynamic literals."""
    expected = [
        {**items[index], "_transaction": transaction["name"]}
        for transaction in transactions
        for index in transaction["items"]
    ]
    groups = [
        (transaction["name"], [items[index] for index in transaction["items"]])
        for transaction in transactions
    ]
    generated = _read_plan(build_recorded_plan(
        name, groups, correlations=correlations, parameterization=parameterization,
        csv_headers=csv_headers, assertions=assertions,
    ))
    issues: list[dict[str, Any]] = []
    request_results: list[dict[str, Any]] = []
    checks_by_request: list[list[dict[str, str]]] = []
    failed = False
    unresolved = 0

    correlation_names = {rule["variable"] for rule in correlations}
    enabled_correlations = {rule["variable"] for rule in correlations if rule.get("enabled", True)}
    parameter_names = {
        rule["variable"] for rule in parameterization.get("parameters", [])
    }
    known_variables = ((set(variables) - correlation_names) | enabled_correlations | parameter_names)
    if enabled_correlations & parameter_names:
        failed = True
        issues.append({
            "severity": "error", "code": "duplicate_variable",
            "message": "A variable name is used by both correlation and CSV parameterization.",
        })
    for rule in parameterization.get("parameters", []):
        if rule["column"] not in csv_headers:
            failed = True
            issues.append({
                "severity": "error", "code": "missing_csv_column",
                "message": f"CSV parameter '{rule['variable']}' refers to missing column '{rule['column']}'.",
            })

    included = {item["index"] for item in expected}
    if not expected:
        failed = True
        issues.append({
            "severity": "error", "code": "empty_selection",
            "message": "No requests are included in the generated plan.",
        })
    for rule in correlations:
        used_targets = set(rule["target_indices"]) & included
        if used_targets and not rule.get("enabled", True):
            failed = True
            issues.append({
                "severity": "error", "code": "disabled_correlation",
                "message": f"Correlation variable ${{{rule['variable']}}} is disabled but used by a kept request.",
            })
        elif used_targets and rule["source_index"] not in included:
            failed = True
            issues.append({
                "severity": "error", "code": "missing_correlation_source",
                "message": f"Correlation source for ${{{rule['variable']}}} is excluded while a target is kept.",
            })

    if len(expected) != len(generated):
        failed = True
        issues.append({
            "severity": "error", "code": "request_count",
            "message": f"The generated plan has {len(generated)} request(s); {len(expected)} kept request(s) were expected.",
        })

    for position, item in enumerate(expected):
        actual = generated[position] if position < len(generated) else None
        checks: list[dict[str, str]] = []

        def add_check(field: str, passed: bool, message: str) -> None:
            nonlocal failed
            checks.append({"field": field, "status": "pass" if passed else "fail", "message": message})
            failed = failed or not passed

        if actual is None:
            add_check("order", False, "Request is missing from the generated plan.")
            request_results.append({
                "index": item["index"], "label": f"Request {item['index'] + 1}", "method": item["method"],
                "recorded_endpoint": _safe_endpoint(item["url"]),
                "generated_endpoint": None, "checks": checks,
            })
            checks_by_request.append(checks)
            continue

        endpoint_ok = _endpoint_key(item["url"]) == _endpoint_key(actual["url"])
        add_check("endpoint", endpoint_ok, "Endpoint matches." if endpoint_ok else "Endpoint differs.")
        method_ok = item["method"].upper() == actual["method"]
        add_check("method", method_ok, "HTTP method matches." if method_ok else "HTTP method differs.")
        headers_ok = _header_pairs(item.get("headers", [])) == _header_pairs(actual["headers"])
        add_check("headers", headers_ok, "Headers match." if headers_ok else "Headers differ.")
        body_ok = item.get("body") == actual["body"]
        add_check("body", body_ok, "Request body matches." if body_ok else "Request body differs.")
        expected_codes = [
            str(check["expected"])
            for rule in assertions
            if (rule["scope"] == "global"
                or (rule["scope"] == "transaction" and rule["transaction"] == item["_transaction"])
                or (rule["scope"] == "request" and rule["request_index"] == item["index"]))
            for check in rule["checks"] if check["type"] == "status_code"
        ]
        status_ok = not item.get("status") or any(
            code in actual["status_codes"]
            or (len(code) == 3 and code[1:] == "xx" and f"{code[0]}\\d\\d" in actual["status_codes"])
            for code in expected_codes
        )
        add_check("response_code", status_ok,
                  "Recorded response code is asserted." if status_ok else "Recorded response code assertion is missing.")
        order_ok = actual["label"] == item["label"]
        add_check("order", order_ok, "Request order matches." if order_ok else "Request order differs.")

        variable_references = {
            variable
            for value in [item.get("url") or "", item.get("body") or "",
                          *(header_value for _, header_value in item.get("headers", []))]
            for variable in _VARIABLE.findall(value)
        }
        unknown = sorted(variable_references - known_variables)
        add_check(
            "tokens", not unknown,
            "JMeter variables are configured." if not unknown
            else f"Unresolved JMeter variable(s): {', '.join('${' + name + '}' for name in unknown)}.",
        )
        for variable in unknown:
            issues.append({
                "severity": "error", "code": "unknown_variable", "request_index": item["index"],
                "message": f"JMeter variable ${{{variable}}} has no enabled extractor or parameter mapping.",
            })
        manual_variables = sorted(
            variable_references - enabled_correlations - parameter_names - set(unknown)
        )
        for variable in manual_variables:
            unresolved += 1
            issues.append({
                "severity": "warning", "code": "manual_variable", "request_index": item["index"],
                "message": f"JMeter variable ${{{variable}}} needs a value in User Defined Variables.",
            })
        endpoint_candidates = _dynamic_candidates(item)
        for field in endpoint_candidates:
            unresolved += 1
            issues.append({
                "severity": "warning", "code": "possible_dynamic_value", "request_index": item["index"],
                "field": field, "message": f"Possible unparameterized dynamic value at {field}.",
            })

        request_results.append({
            "index": item["index"], "label": f"Request {item['index'] + 1}", "method": item["method"],
            "recorded_endpoint": _safe_endpoint(item["url"]),
            "generated_endpoint": _safe_endpoint(actual["url"]), "checks": checks,
        })
        checks_by_request.append(checks)

    status = "Fail" if failed else "Pass with warnings" if unresolved else "Pass"
    return {
        "status": status,
        "summary": {
            "requests_checked": len(expected),
            "requests_passed": sum(all(check["status"] == "pass" for check in checks) for checks in checks_by_request),
            "requests_failed": sum(any(check["status"] == "fail" for check in checks) for checks in checks_by_request),
            "unresolved_dynamic_values": unresolved,
        },
        "requests": request_results,
        "issues": issues,
    }
