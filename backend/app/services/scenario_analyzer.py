"""Results of a scenario run: per business function, per group, SLAs and the goal.

A results file mixes two kinds of rows: requests (HTTP samples) and the rows each Transaction
Controller writes for a whole business function. They are kept apart, so requests are not
counted twice: request rows give hits per second, errors and status codes; transaction rows
give LoadRunner's transaction summary (min, average, max, 90th percentile, pass/fail, TPS).
"""
from __future__ import annotations

import re
import statistics
from typing import Any

from ..models.scenario import ScenarioConfig, SlaRule
from ..models.test_config import Thresholds
from .result_analyzer import Sample, percentile, summarize

_NUMBER = re.compile(r"^\d{2,} ")
_THREAD_SUFFIX = re.compile(r" \d+-\d+$")
METRIC_LABELS = {"p90": "90th percentile", "p95": "95th percentile", "p99": "99th percentile", "avg": "average",
                 "max": "maximum", "error_rate": "error rate", "tps_min": "transactions per second"}


def display_name(label: str) -> str:
    return _NUMBER.sub("", label)


def split(samples: list[Sample]) -> tuple[list[Sample], list[Sample]]:
    return [s for s in samples if not s.transaction], [s for s in samples if s.transaction]


def label_stats(samples: list[Sample], duration_s: float) -> list[dict[str, Any]]:
    by_label: dict[str, list[Sample]] = {}
    for s in samples:
        by_label.setdefault(s.label, []).append(s)
    out = []
    for label, group in by_label.items():
        times = sorted(x.elapsed for x in group)
        failed = sum(1 for x in group if not x.success)
        out.append({
            "name": label, "display": display_name(label), "count": len(group),
            "passed": len(group) - failed, "failed": failed, "error_rate_percent": round(100 * failed / len(group), 2),
            "min": times[0], "avg": round(statistics.fmean(times), 1), "max": times[-1],
            "std": round(statistics.pstdev(times), 1) if len(times) > 1 else 0.0,
            "p50": percentile(times, 50), "p90": percentile(times, 90), "p95": percentile(times, 95),
            "p99": percentile(times, 99), "tps": round(len(group) / duration_s, 3),
            "bytes_avg": round(statistics.fmean(x.bytes for x in group)),
        })
    return sorted(out, key=lambda r: r["name"])


def group_of(thread: str, names: list[str]) -> str | None:
    """Thread names are '<thread group> 1-3'; thread groups are named after their group."""
    tg = _THREAD_SUFFIX.sub("", thread)
    matches = [n for n in names if tg == n or tg.startswith(n + " ")]
    return max(matches, key=len) if matches else None


def group_stats(requests: list[Sample], names: list[str]) -> list[dict[str, Any]]:
    per = {n: {"name": n, "requests": 0, "failed": 0, "peak_users": 0} for n in names}
    peaks: dict[str, int] = {}
    for s in requests:
        group = group_of(s.thread, names)
        if group is None:
            continue
        per[group]["requests"] += 1
        per[group]["failed"] += 0 if s.success else 1
        tg = _THREAD_SUFFIX.sub("", s.thread)
        peaks[tg] = max(peaks.get(tg, 0), s.group_users)
    for tg, peak in peaks.items():
        group = group_of(f"{tg} 1-1", names)
        if group:
            per[group]["peak_users"] += peak    # cohorts of a group are all running at its peak
    for g in per.values():
        g["error_rate_percent"] = round(100 * g["failed"] / g["requests"], 2) if g["requests"] else 0.0
    return list(per.values())


def users_timeline(requests: list[Sample], names: list[str], bucket_seconds: int, t0: int) -> dict[str, list[dict]]:
    """Running users per group in each interval (the sum of its thread groups' active threads)."""
    buckets: dict[int, dict[str, int]] = {}
    for s in requests:
        tg = _THREAD_SUFFIX.sub("", s.thread)
        b = buckets.setdefault((s.ts - t0) // (bucket_seconds * 1000), {})
        b[tg] = max(b.get(tg, 0), s.group_users)
    out: dict[str, list[dict]] = {n: [] for n in names}
    for idx in sorted(buckets):
        totals = {n: 0 for n in names}
        for tg, users in buckets[idx].items():
            group = group_of(f"{tg} 1-1", names)
            if group:
                totals[group] += users
        for n in names:
            out[n].append({"t": idx * bucket_seconds, "users": totals[n]})
    return out


def transaction_timeline(transactions: list[Sample], bucket_seconds: int, t0: int) -> dict[str, list[dict]]:
    by_name: dict[str, dict[int, list[Sample]]] = {}
    for s in transactions:
        by_name.setdefault(s.label, {}).setdefault((s.ts - t0) // (bucket_seconds * 1000), []).append(s)
    out = {}
    for label, buckets in sorted(by_name.items()):
        points = []
        for idx in sorted(buckets):
            group = buckets[idx]
            times = sorted(x.elapsed for x in group)
            points.append({"t": idx * bucket_seconds, "count": len(group), "tps": round(len(group) / bucket_seconds, 3),
                           "avg_ms": round(statistics.fmean(times), 1), "p90_ms": percentile(times, 90),
                           "errors": sum(1 for x in group if not x.success)})
        out[label] = points
    return out


def evaluate_sla(rules: list[SlaRule], tx_stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for rule in rules:
        targets = tx_stats if rule.transaction == "*" else [
            t for t in tx_stats if t["display"].lower() == rule.transaction.lower() or t["name"] == rule.transaction]
        if not targets:
            results.append({"transaction": rule.transaction, "metric": rule.metric, "limit": rule.limit,
                            "actual": None, "status": "no_data"})
            continue
        for t in targets:
            actual = {"p90": t["p90"], "p95": t["p95"], "p99": t["p99"], "avg": t["avg"], "max": t["max"],
                      "error_rate": t["error_rate_percent"], "tps_min": t["tps"]}[rule.metric]
            ok = actual >= rule.limit if rule.metric == "tps_min" else actual <= rule.limit
            results.append({"transaction": t["display"], "metric": rule.metric, "limit": rule.limit,
                            "actual": actual, "status": "pass" if ok else "fail"})
    return results


def goal_result(cfg: ScenarioConfig, requests: list[Sample], transactions: list[Sample], start: int, end: int) -> dict[str, Any]:
    """Throughput reached once every user is running (after the ramp-up)."""
    goal = cfg.goal
    steady_from = start + (cfg.schedule.start_delay_seconds + cfg.schedule.ramp_up_seconds) * 1000
    pool = requests if goal.type == "hits_per_second" else transactions
    steady = [s for s in pool if s.ts >= steady_from]
    window = max((end - steady_from) / 1000, 0.001)
    actual = round(len(steady) / window, 2) if steady else 0.0
    return {"type": goal.type, "target": goal.target, "actual": actual, "reached": actual >= 0.9 * goal.target}


def scenario_summary(samples: list[Sample], cfg: ScenarioConfig, group_names: list[str]) -> dict[str, Any]:
    requests, transactions = split(samples)
    base = summarize(requests, Thresholds(max_error_rate_percent=100, max_p95_ms=None))
    if not requests:
        return {"requests": base, "transactions": [], "request_labels": [], "groups": group_stats([], group_names),
                "sla": evaluate_sla(cfg.sla, []), "goal": None, "warnings": [], "verdict": "no_data"}
    start = min(s.ts for s in samples)
    end = max(s.ts + s.elapsed for s in samples)
    duration = max((end - start) / 1000, 0.001)
    tx_stats = label_stats(transactions, duration)
    sla = evaluate_sla(cfg.sla, tx_stats)
    goal = goal_result(cfg, requests, transactions, start, end) if cfg.mode == "goal" and cfg.goal else None

    warnings = list(base["warnings"])
    for r in sla:
        if r["status"] == "fail":
            unit = "%" if r["metric"] == "error_rate" else "/s" if r["metric"] == "tps_min" else " ms"
            warnings.append({"severity": "critical", "code": "sla",
                             "message": f"SLA failed: {r['transaction']} {METRIC_LABELS[r['metric']]} was "
                                        f"{r['actual']}{unit}, limit {r['limit']:g}{unit}."})
        elif r["status"] == "no_data":
            warnings.append({"severity": "warning", "code": "sla_no_data",
                             "message": f"SLA on {r['transaction']}: that business function produced no results."})
    if goal and not goal["reached"]:
        unit = "requests" if goal["type"] == "hits_per_second" else "transactions"
        warnings.append({"severity": "warning", "code": "goal",
                         "message": f"The goal of {goal['target']:g} {unit} per second was not reached "
                                    f"({goal['actual']:g}/s once all users were running). Allow more users, "
                                    "or check errors and response times."})
    return {
        "requests": base,
        "transactions": tx_stats,
        "request_labels": label_stats(requests, duration),
        "groups": group_stats(requests, group_names),
        "sla": sla,
        "goal": goal,
        "warnings": warnings,
        "verdict": "fail" if any(r["status"] == "fail" for r in sla) else "pass",
    }
