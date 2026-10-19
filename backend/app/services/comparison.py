"""Compare completed performance-test summaries with a baseline run."""
from __future__ import annotations

from typing import Any


def _delta(current: float | int | None, baseline: float | int | None) -> dict[str, Any]:
    if current is None or baseline is None:
        return {"current": current, "baseline": baseline, "absolute": None, "percent": None}
    absolute = current - baseline
    percent = round(absolute / baseline * 100, 2) if baseline else None
    return {
        "current": current,
        "baseline": baseline,
        "absolute": absolute,
        "percent": percent,
    }


def compare_summaries(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    current_rt = current.get("response_time_ms") or {}
    baseline_rt = baseline.get("response_time_ms") or {}
    metrics = {
        "error_rate_percent": _delta(
            current.get("error_rate_percent"), baseline.get("error_rate_percent")
        ),
        "throughput_rps": _delta(current.get("throughput_rps"), baseline.get("throughput_rps")),
        "p50_ms": _delta(current_rt.get("p50"), baseline_rt.get("p50")),
        "p95_ms": _delta(current_rt.get("p95"), baseline_rt.get("p95")),
        "p99_ms": _delta(current_rt.get("p99"), baseline_rt.get("p99")),
    }
    regressions = []
    error_delta = metrics["error_rate_percent"]
    if (error_delta["percent"] or 0) > 0 or (
        error_delta["percent"] is None and (error_delta["absolute"] or 0) > 0
    ):
        regressions.append("error_rate_percent")
    for name in ("p50_ms", "p95_ms", "p99_ms"):
        delta = metrics[name]
        if (delta["percent"] or 0) > 0 or (
            delta["percent"] is None and (delta["absolute"] or 0) > 0
        ):
            regressions.append(name)
    throughput_delta = metrics["throughput_rps"]
    if (throughput_delta["percent"] or 0) < 0 or (
        throughput_delta["percent"] is None and (throughput_delta["absolute"] or 0) < 0
    ):
        regressions.append("throughput_rps")
    return {
        "status": "regressed" if regressions else "stable_or_improved",
        "regressions": regressions,
        "metrics": metrics,
    }
