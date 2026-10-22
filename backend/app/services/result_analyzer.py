"""Parse JMeter JTL (CSV) results into metrics, a timeline and plain-language warnings."""
from __future__ import annotations

import csv
import io
import math
import statistics
import threading
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models.test_config import StopCondition, Thresholds

CACHE_FILES = 4


@dataclass(slots=True)
class Sample:
    ts: int          # epoch ms, sample start
    elapsed: int     # ms
    latency: int     # ms
    code: str
    message: str
    success: bool
    failure: str
    users: int
    label: str = ""
    thread: str = ""
    group_users: int = 0
    bytes: int = 0
    sent_bytes: int = 0
    connect: int = 0
    transaction: bool = False   # a Transaction Controller's own row (one business function)


def _sample(row: dict[str, str]) -> Sample | None:
    # A live run can leave a half-written last line: skip anything without a clear result.
    if (row.get("success") or "").lower() not in ("true", "false"):
        return None
    try:
        message = row.get("responseMessage") or ""
        return Sample(
            ts=int(row["timeStamp"]),
            elapsed=int(row["elapsed"]),
            latency=int(row.get("Latency") or row["elapsed"]),
            code=row.get("responseCode") or "",
            message=message,
            success=(row.get("success") or "").lower() == "true",
            failure=row.get("failureMessage") or "",
            users=int(row.get("allThreads") or 0),
            label=row.get("label") or "",
            thread=row.get("threadName") or "",
            group_users=int(row.get("grpThreads") or 0),
            bytes=int(row.get("bytes") or 0),
            sent_bytes=int(row.get("sentBytes") or 0),
            connect=int(row.get("Connect") or 0),
            transaction=message.startswith("Number of samples in transaction"),
        )
    except (KeyError, ValueError, TypeError):
        return None


class JtlTail:
    """Read a JTL CSV that JMeter is still writing, a piece at a time.

    Only complete lines are consumed, so a half-written last line is read on the next call.
    A record that spans lines (a quoted message with a newline) is kept until it is complete.
    """

    def __init__(self, path: Path, *, keep: bool = True):
        self.path = path
        self.offset = 0
        self.header: list[str] | None = None
        self.samples: list[Sample] = []
        self.keep = keep

    def read(self) -> list[Sample]:
        """New samples since the last call (also kept in self.samples unless keep=False)."""
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except FileNotFoundError:
            return []
        end = chunk.rfind(b"\n")
        while end >= 0 and chunk.count(b'"', 0, end + 1) % 2:   # a quoted field is still open
            end = chunk.rfind(b"\n", 0, end)
        if end < 0:
            return []
        self.offset += end + 1
        reader = csv.reader(io.StringIO(chunk[:end + 1].decode("utf-8", errors="replace"), newline=""))
        fresh = []
        for values in reader:
            if self.header is None:
                self.header = values
                continue
            sample = _sample(dict(zip(self.header, values)))
            if sample is not None:
                fresh.append(sample)
        if self.keep:
            self.samples.extend(fresh)
        return fresh


class BreakingPoint:
    """Watches a running test and says when it has gone past its stop_when limits.

    The limits apply to the last `for_seconds` of results: the error rate over that window, or
    its 95th percentile. The first time either is exceeded (with enough requests to judge), the
    test should stop; the load at that moment is its breaking point.
    """

    MIN_REQUESTS = 10

    def __init__(self, path: Path, condition: StopCondition):
        self.tail = JtlTail(path, keep=False)
        self.condition = condition
        self.window: deque[Sample] = deque()
        self.t0: int | None = None

    def check(self) -> dict[str, Any] | None:
        fresh = [s for s in self.tail.read() if not s.transaction]
        if not fresh:
            return None
        self.window.extend(sorted(fresh, key=lambda s: s.ts))
        if self.t0 is None:
            self.t0 = self.window[0].ts
        latest = max(s.ts for s in self.window)
        span = self.condition.for_seconds * 1000
        while self.window and self.window[0].ts < latest - span:
            self.window.popleft()
        if latest - self.t0 < span or len(self.window) < self.MIN_REQUESTS:
            return None                       # not enough of the test has run to judge
        failed = sum(1 for s in self.window if not s.success)
        error_rate = round(100 * failed / len(self.window), 2)
        p95 = percentile(sorted(s.elapsed for s in self.window), 95)
        users = max(s.users for s in self.window)
        c = self.condition
        if c.error_rate_percent is not None and error_rate > c.error_rate_percent:
            reason = f"errors reached {error_rate}% over the last {c.for_seconds} s (limit {c.error_rate_percent:g}%)"
        elif c.p95_ms is not None and p95 > c.p95_ms:
            reason = f"the 95th percentile reached {p95} ms over the last {c.for_seconds} s (limit {c.p95_ms} ms)"
        else:
            return None
        return {"reason": reason, "users": users, "t": round((latest - self.t0) / 1000),
                "error_rate_percent": error_rate, "p95_ms": p95}


def add_breaking_point(summary: dict[str, Any], point: dict[str, Any]) -> None:
    """Record in a finished run's summary that it stopped itself at its breaking point."""
    summary["breaking_point"] = point
    summary["warnings"].insert(0, {
        "severity": "critical", "code": "breaking_point",
        "message": f"Stopped at the breaking point: {point['reason']}, with {point['users']} users running "
                   f"after {point['t']} s."})
    summary["verdict"] = "fail"


_cache: OrderedDict[str, tuple[tuple[int, int], JtlTail]] = OrderedDict()
_cache_lock = threading.Lock()


def parse_jtl(path: Path) -> list[Sample]:
    """Read a JTL CSV. Tolerates a partially written final line (live runs).

    A file that only grew since the last call is read from where that call stopped, so polling
    a live run does not re-read the whole file every time.
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return []
    key = str(path.resolve())
    identity = (stat.st_dev, stat.st_ino)        # a new file at the same path starts over
    with _cache_lock:
        cached = _cache.get(key)
        if cached is None or cached[0] != identity or stat.st_size < cached[1].offset:
            cached = (identity, JtlTail(path))
        _cache[key] = cached
        _cache.move_to_end(key)
        while len(_cache) > CACHE_FILES:
            _cache.popitem(last=False)
        tail = cached[1]
        if stat.st_size > tail.offset:
            tail.read()
        return list(tail.samples)


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


def _is_status_failure(s: Sample) -> bool:
    """Failed on its status code: an HTTP error, or a code other than the expected one."""
    if s.success or _is_connection_failure(s):
        return False
    if s.failure.lower().startswith("test failed: code"):          # the expected-status assertion
        return True
    return not s.failure and (_http_status(s) or 0) >= 400


def _is_assertion_failure(s: Sample) -> bool:
    """Failed a check on its content, time or size (requirements 8.6): counted apart from HTTP errors."""
    return (not s.success) and bool(s.failure) and not _is_status_failure(s) and not _is_connection_failure(s)


def failure_kinds(samples: list[Sample]) -> dict[str, int]:
    return {"status_code_failures": sum(1 for s in samples if _is_status_failure(s)),
            "assertion_failures": sum(1 for s in samples if _is_assertion_failure(s)),
            "connection_failures": sum(1 for s in samples if _is_connection_failure(s))}


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
            "status_codes": {}, "status_code_failures": 0, "assertion_failures": 0, "connection_failures": 0,
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
        **failure_kinds(samples),
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
        msg = f"p95 response time was {p95} ms, above the {th.max_p95_ms} ms limit."
        first = next((b for b in buckets if b["p95_ms"] > th.max_p95_ms and b["requests"] >= 5), None)
        if first:
            msg += f" It first went over the limit at {first['t']}s with {first['users']} users active."
        add("critical", "p95_latency", msg)

    server_errors = sum(1 for s in samples if (c := _http_status(s)) is not None and c >= 500)
    if server_errors:
        add("warning", "http_5xx", f"{server_errors} requests returned HTTP 5xx responses.")

    if summary["connection_failures"]:
        add("warning", "connection_failures",
            f"{summary['connection_failures']} requests failed to connect or timed out.")

    if summary["status_code_failures"]:
        add("warning", "unexpected_status",
            f"{summary['status_code_failures']} responses did not have the expected status code.")

    if summary["assertion_failures"]:
        add("warning", "assertion_failures",
            f"{summary['assertion_failures']} responses failed a check on their content, time or size "
            "(counted apart from status code errors).")

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
