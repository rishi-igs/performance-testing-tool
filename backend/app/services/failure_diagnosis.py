"""Rule-based guidance for failed debug-run samples."""
from __future__ import annotations

from typing import Any


def diagnose(samples: list[dict[str, Any]]) -> dict[str, Any]:
    failures = [sample for sample in samples if not sample["success"]]
    if not failures:
        return {"first_failing_request": None, "failures": [], "message": "No failed requests to diagnose."}
    reports = []
    for sample in failures:
        code = str(sample["response"]["status_code"])
        text = (sample["failure"] + " " + sample["response"]["body"]).lower()
        suggestions = []
        if code in {"401", "403"}:
            suggestions.append({
                "cause": "Authentication or session data may be missing or stale.",
                "fix": "Review the source response extractor and the target request's authorization or cookie handling.",
                "editor": "correlations",
            })
        if code == "403" and any(word in text for word in ("csrf", "xsrf", "cross-site")):
            suggestions.append({
                "cause": "The server rejected the CSRF token.",
                "fix": "Correlate the latest CSRF token and ensure it is sent in the required header or body field.",
                "editor": "correlations",
            })
        if code == "400":
            suggestions.append({
                "cause": "The server rejected a request field or payload.",
                "fix": "Check CSV parameter mappings and compare the request body with the expected schema.",
                "editor": "parameters",
            })
        if code == "404":
            suggestions.append({
                "cause": "A recorded resource or identifier may not exist in this environment.",
                "fix": "Parameterize identifiers that must be created or looked up during the flow.",
                "editor": "parameters",
            })
        if "not_found" in text or "not found" in text:
            suggestions.append({
                "cause": "A response extractor may not have matched the current response.",
                "fix": "Check the JSONPath and source response for the configured correlation.",
                "editor": "correlations",
            })
        if any(not assertion["passed"] for assertion in sample["assertions"]):
            suggestions.append({
                "cause": "The response did not satisfy an assertion.",
                "fix": "Review the expected status, response text, JSONPath, header, duration, or size check.",
                "editor": "assertions",
            })
        if code.lower().startswith("non http") or any(
            word in text for word in ("connection refused", "timed out", "unknownhost", "connectexception")
        ):
            suggestions.append({
                "cause": "The target could not be reached or did not respond before the timeout.",
                "fix": "Check the target URL, service availability, firewall, and response timeout.",
                "editor": "target",
            })
        if not suggestions:
            suggestions.append({
                "cause": "The response failed, but no specific cause matched the available diagnostics.",
                "fix": "Inspect the sanitized request and response details and the JMeter log.",
                "editor": "samples",
            })
        reports.append({
            "index": sample["index"],
            "label": sample["label"],
            "status_code": code,
            "first_failure": sample["index"] == failures[0]["index"],
            "suggestions": suggestions,
        })
    return {
        "first_failing_request": failures[0]["index"],
        "message": f"{len(failures)} request sample(s) failed; later failures may result from the first failure.",
        "failures": reports,
    }
