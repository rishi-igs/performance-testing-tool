"""Parse JMeter JTL (CSV) results into metrics, a timeline and plain-language warnings."""
from __future__ import annotations

import csv
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models.test_config import Thresholds


@dataclass
class Sample:
    ts: int          # epoch ms, sample start
    elapsed: int     # ms
    latency: int     # ms
    code: str
    message: str
    success: bool
    failure: str
    users: int


def parse_jtl(path: Path) -> list[Sample]:
    """Read a JTL CSV. Tolerates a partially written final line (live runs)."""
    if not path.exists():
        return []
    samples: list[Sample] = []
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            # A live run can leave a half-written last line: skip anything without a clear result.
            if (row.get("success") or "").lower() not in ("true", "false"):
                continue
            try:
                samples.append(Sample(
                    ts=int(row["timeStamp"]),
                    elapsed=int(row["elapsed"]),
                    latency=int(row.get("Latency") or row["elapsed"]),
                    code=row.get("responseCode") or "",
                    message=row.get("responseMessage") or "",
                    success=(row.get("success") or "").lower() == "true",
                    failure=row.get("failureMessage") or "",
                    users=int(row.get("allThreads") or 0),
                ))
            except (KeyError, ValueError, TypeError):
                continue
    return samples


def percentile(sorted_values: list[int], p: float) -> int:
    """Nearest-rank percentile on an already sorted list."""
    if not sorted_values:
        return 0
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _is_connection_failure(s: Sample) -> bool:
    text = f"{s.code} {s.message}".lower()
    return (not s.success) and (
        s.code.lower().startswith("non http")
        or any(k in text for k in ("connectexception", "connection refused", "connect timed out", "unknownhost", "sockettimeout"))
    )


def _is_assertion_failure(s: Sample) -> bool:
    return (not s.success) and s.failure.lower().startswith("test failed")


def _http_status(s: Sample) -> int | None:
    return int(s.code) if s.code.isdigit() else None


def timeline(samples: list[Sample], bucket_seconds: int = 5) -> list[dict[str, Any]]:
    if not samples:
        return []
    t0 = min(s.ts for s in samples)
    buckets: dict[int, list[Sample]] = {}
    for s in samples:
        buckets.setdefault((s.ts - t0) // (bucket_seconds * 1000), []).append(s)
    out = []
    for idx in sorted(buckets):
        group = buckets[idx]
        times = sorted(s.elapsed for s in group)
        errors = sum(1 for s in group if not s.success)
        out.append({
            "t": idx * bucket_seconds,
            "requests": len(group),
            "errors": errors,
            "error_rate_percent": round(100 * errors / len(group), 2),
            "avg_ms": round(statistics.fmean(times), 1),
            "p95_ms": percentile(times, 95),
            "rps": round(len(group) / bucket_seconds, 2),
            "users": max(s.users for s in group),
        })
    return out


def summarize(samples: list[Sample], thresholds: Thresholds) -> dict[str, Any]:
    if not samples:
        return {
            "total_requests": 0, "successful_requests": 0, "failed_requests": 0,
            "error_rate_percent": 0.0, "response_time_ms": None, "latency_avg_ms": None,
            "throughput_rps": 0.0, "duration_seconds": 0.0, "active_users": 0, "peak_users": 0,
            "status_codes": {}, "assertion_failures": 0, "connection_failures": 0,
            "warnings": [], "verdict": "no_data",
        }

    total = len(samples)
    failed = sum(1 for s in samples if not s.success)
    times = sorted(s.elapsed for s in samples)
    start = min(s.ts for s in samples)
    end = max(s.ts + s.elapsed for s in samples)
    duration = max((end - start) / 1000, 0.001)
    ordered = sorted(samples, key=lambda s: s.ts)

    summary: dict[str, Any] = {
        "total_requests": total,
        "successful_requests": total - failed,
        "failed_requests": failed,
        "error_rate_percent": round(100 * failed / total, 2),
        "response_time_ms": {
            "avg": round(statistics.fmean(times), 1),
            "min": times[0],
            "max": times[-1],
            "p50": percentile(times, 50),
            "p95": percentile(times, 95),
            "p99": percentile(times, 99),
        },
        "latency_avg_ms": round(statistics.fmean(s.latency for s in samples), 1),
        "throughput_rps": round(total / duration, 2),
        "duration_seconds": round(duration, 1),
        "active_users": ordered[-1].users,
        "peak_users": max(s.users for s in samples),
        "status_codes": dict(Counter(s.code or "unknown" for s in samples).most_common()),
        "assertion_failures": sum(1 for s in samples if _is_assertion_failure(s)),
        "connection_failures": sum(1 for s in samples if _is_connection_failure(s)),
    }
    summary["warnings"] = build_warnings(samples, summary, timeline(samples), thresholds)
    summary["verdict"] = "fail" if any(w["severity"] == "critical" for w in summary["warnings"]) else "pass"
    return summary


def build_warnings(
    samples: list[Sample], summary: dict[str, Any], buckets: list[dict[str, Any]], th: Thresholds
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        warnings.append({"severity": severity, "code": code, "message": message})

    # Error rate over the limit, with the load level where it first happened
    if summary["error_rate_percent"] > th.max_error_rate_percent:
        first = next((b for b in buckets if b["error_rate_percent"] > th.max_error_rate_percent), None)
        msg = (f"Overall error rate was {summary['error_rate_percent']}%, above the "
               f"{th.max_error_rate_percent}% limit.")
        if first:
            msg = (f"Error rate reached {first['error_rate_percent']}% when {first['users']} users were "
                   f"active (at {first['t']}s). Overall error rate was {summary['error_rate_percent']}%, "
                   f"above the {th.max_error_rate_percent}% limit.")
        if summary["connection_failures"] and summary["connection_failures"] >= summary["failed_requests"] / 2:
            msg += " Most failures were connection errors or timeouts: check server capacity, connection limits and pools."
        add("critical", "error_rate", msg)

    p95 = summary["response_time_ms"]["p95"]
    if th.max_p95_ms is not None and p95 > th.max_p95_ms:
        add("critical", "p95_latency", f"p95 response time was {p95} ms, above the {th.max_p95_ms} ms limit.")

    server_errors = sum(1 for s in samples if (c := _http_status(s)) is not None and c >= 500)
    if server_errors:
        add("warning", "http_5xx", f"{server_errors} requests returned HTTP 5xx responses.")

    if summary["connection_failures"]:
        add("warning", "connection_failures",
            f"{summary['connection_failures']} requests failed to connect or timed out.")

    if summary["assertion_failures"]:
        add("warning", "assertion_failures",
            f"{summary['assertion_failures']} responses did not match the expected status codes.")

    # Sudden degradation: a bucket whose p95 is far above the earlier typical level
    for i in range(3, len(buckets)):
        baseline = statistics.median(b["p95_ms"] for b in buckets[:i])
        cur = buckets[i]
        if cur["p95_ms"] > max(3 * baseline, 200) and cur["requests"] >= 5:
            add("warning", "degradation",
                f"Response time degraded suddenly at {cur['t']}s: p95 rose from about {int(baseline)} ms "
                f"to {cur['p95_ms']} ms with {cur['users']} users active.")
            break
    return warnings
