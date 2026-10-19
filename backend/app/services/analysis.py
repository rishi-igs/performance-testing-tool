"""Analysis of a run, like LoadRunner Analysis: graph series, error statistics, run comparison.

Every graph is a series of (seconds since the start, value) points, so any of them can be laid
over another (LoadRunner's "merge graphs"), and two runs can be lined up by elapsed time.
"""
from __future__ import annotations

import statistics
from typing import Any

from .result_analyzer import Sample, percentile
from .scenario_analyzer import _THREAD_SUFFIX, display_name, group_of, label_stats, split

MAX_LABEL_SERIES = 20
REGRESSION_PERCENT = 10.0       # slower by more than this (and by 50 ms) counts as a regression
REGRESSION_MS = 50


def _buckets(samples: list[Sample], bucket_seconds: int, t0: int) -> dict[int, list[Sample]]:
    out: dict[int, list[Sample]] = {}
    for s in samples:
        out.setdefault((s.ts - t0) // (bucket_seconds * 1000), []).append(s)
    return out


def _series(sid: str, label: str, unit: str, category: str, points: list[dict[str, float]]) -> dict[str, Any]:
    return {"id": sid, "label": label, "unit": unit, "category": category, "points": points}


def series(samples: list[Sample], *, bucket_seconds: int, group_names: list[str] | None = None,
           monitors: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every graph this run can show."""
    if not samples:
        return []
    requests, transactions = split(samples)
    t0 = min(s.ts for s in samples)
    reqb = _buckets(requests, bucket_seconds, t0)
    keys = sorted(reqb)
    per_s = lambda n: round(n / bucket_seconds, 3)  # noqa: E731
    pt = lambda idx, v: {"t": idx * bucket_seconds, "v": v}  # noqa: E731
    out = [
        _series("users", "Running users", "users", "Load", [pt(i, max(s.users for s in reqb[i])) for i in keys]),
        _series("hits", "Hits per second", "/s", "Throughput", [pt(i, per_s(len(reqb[i]))) for i in keys]),
        _series("throughput", "Throughput (KB/s)", "KB/s", "Throughput",
                [pt(i, round(sum(s.bytes for s in reqb[i]) / 1024 / bucket_seconds, 2)) for i in keys]),
        _series("errors", "Errors per second", "/s", "Errors",
                [pt(i, per_s(sum(1 for s in reqb[i] if not s.success))) for i in keys]),
        _series("rt:avg", "Average response time, all requests", "ms", "Response time",
                [pt(i, round(statistics.fmean(s.elapsed for s in reqb[i]), 1)) for i in keys]),
        _series("rt:p90", "90th percentile, all requests", "ms", "Response time",
                [pt(i, percentile(sorted(s.elapsed for s in reqb[i]), 90)) for i in keys]),
    ]
    if group_names:
        for name in group_names:
            points = []
            for i in keys:
                peaks: dict[str, int] = {}
                for s in reqb[i]:
                    if group_of(s.thread, group_names) == name:
                        tg = _THREAD_SUFFIX.sub("", s.thread)
                        peaks[tg] = max(peaks.get(tg, 0), s.group_users)
                points.append(pt(i, sum(peaks.values())))
            out.append(_series(f"users:{name}", f"Running users: {name}", "users", "Load", points))
    codes = sorted({s.code for s in requests})
    for code in codes:
        out.append(_series(f"code:{code}", f"HTTP {code} per second" if code.isdigit() else f"{code[:40]} per second",
                           "/s", "Errors" if not code.startswith(("2", "3")) else "Throughput",
                           [pt(i, per_s(sum(1 for s in reqb[i] if s.code == code))) for i in keys]))
    by_tx: dict[str, list[Sample]] = {}
    for s in transactions:
        by_tx.setdefault(s.label, []).append(s)
    for name, group in sorted(by_tx.items()):
        b = _buckets(group, bucket_seconds, t0)
        ks = sorted(b)
        shown = display_name(name)
        out += [
            _series(f"tx:{name}:avg", f"{shown}: average", "ms", "Business functions",
                    [pt(i, round(statistics.fmean(s.elapsed for s in b[i]), 1)) for i in ks]),
            _series(f"tx:{name}:p90", f"{shown}: 90th percentile", "ms", "Business functions",
                    [pt(i, percentile(sorted(s.elapsed for s in b[i]), 90)) for i in ks]),
            _series(f"tx:{name}:tps", f"{shown}: per second", "/s", "Business functions",
                    [pt(i, per_s(len(b[i]))) for i in ks]),
            _series(f"tx:{name}:failed", f"{shown}: failed per second", "/s", "Errors",
                    [pt(i, per_s(sum(1 for s in b[i] if not s.success))) for i in ks]),
        ]
    counts: dict[str, int] = {}
    for s in requests:
        counts[s.label] = counts.get(s.label, 0) + 1
    for label in sorted(counts, key=lambda k: -counts[k])[:MAX_LABEL_SERIES]:
        b = _buckets([s for s in requests if s.label == label], bucket_seconds, t0)
        out.append(_series(f"req:{label}:avg", f"{label}: average", "ms", "Requests",
                           [pt(i, round(statistics.fmean(s.elapsed for s in b[i]), 1)) for i in sorted(b)]))
    for m in monitors or []:
        out.append(_series(f"mon:{m['monitor']}:{m['metric']}", f"{m['monitor']}: {m['label']}", m["unit"], "Servers",
                           [{"t": round((ts - t0) / 1000), "v": v} for ts, v in m["points"]]))
    return out


def errors(samples: list[Sample]) -> list[dict[str, Any]]:
    """Error statistics: failures grouped by code and message, with where and when they started."""
    if not samples:
        return []
    t0 = min(s.ts for s in samples)
    requests, _ = split(samples)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for s in requests:
        if s.success:
            continue
        reason = (s.failure or s.message or "").split("\n")[0][:200]
        g = groups.setdefault((s.code, reason), {"code": s.code, "message": reason, "count": 0,
                                                "first_seconds": round((s.ts - t0) / 1000), "requests": {}})
        g["count"] += 1
        g["requests"][s.label] = g["requests"].get(s.label, 0) + 1
    out = []
    for g in sorted(groups.values(), key=lambda g: -g["count"]):
        g["requests"] = [{"label": k, "count": v} for k, v in sorted(g["requests"].items(), key=lambda kv: -kv[1])[:5]]
        out.append(g)
    return out[:50]


def _stats_for_compare(samples: list[Sample]) -> tuple[list[dict[str, Any]], str]:
    requests, transactions = split(samples)
    if not samples:
        return [], "transactions"
    duration = max((max(s.ts + s.elapsed for s in samples) - min(s.ts for s in samples)) / 1000, 0.001)
    if transactions:
        return label_stats(transactions, duration), "transactions"
    return label_stats(requests, duration), "requests"


def _change(a: float | None, b: float | None) -> float | None:
    if a in (None, 0) or b is None:
        return None
    return round(100 * (b - a) / a, 1)


def regression_verdict(a: dict[str, Any], b: dict[str, Any]) -> str | None:
    """One business function of run B against baseline A: 'slower', 'faster', 'more errors' or None."""
    verdict = None
    delta, change = b["p90"] - a["p90"], _change(a["p90"], b["p90"]) or 0
    if change > REGRESSION_PERCENT and delta > REGRESSION_MS:
        verdict = "slower"
    elif change < -REGRESSION_PERCENT and -delta > REGRESSION_MS:
        verdict = "faster"
    if b["error_rate_percent"] > a["error_rate_percent"] + 1:
        verdict = "more errors"
    return verdict


def compare(a_samples: list[Sample], b_samples: list[Sample], *, bucket_seconds: int) -> dict[str, Any]:
    """Run B against baseline A: per business function (or request), then aligned graphs."""
    a_stats, a_kind = _stats_for_compare(a_samples)
    b_stats, b_kind = _stats_for_compare(b_samples)
    a_by = {s["display"]: s for s in a_stats}
    b_by = {s["display"]: s for s in b_stats}
    rows = []
    for name in sorted(set(a_by) | set(b_by), key=lambda n: (a_by.get(n) or b_by.get(n))["name"]):
        a, b = a_by.get(name), b_by.get(name)
        row: dict[str, Any] = {"name": name, "in": "both" if a and b else "a" if a else "b"}
        for metric in ("count", "avg", "p90", "p95", "max", "tps", "error_rate_percent"):
            av, bv = (a or {}).get(metric), (b or {}).get(metric)
            row[metric] = {"a": av, "b": bv, "change_percent": _change(av, bv)}
        row["verdict"] = regression_verdict(a, b) if a and b else None
        rows.append(row)

    def aligned(samples: list[Sample]) -> dict[str, list[dict[str, float]]]:
        return {s["id"]: s["points"] for s in series(samples, bucket_seconds=bucket_seconds)
                if s["id"] in ("users", "hits", "rt:p90", "errors")}

    return {"kind": a_kind if a_kind == b_kind else "mixed", "rows": rows,
            "regressions": sum(1 for r in rows if r["verdict"] in ("slower", "more errors")),
            "series": {"a": aligned(a_samples), "b": aligned(b_samples)}}


TREND_METRICS = ("avg", "p90", "p95", "tps", "error_rate_percent")


def trends(runs: list[dict[str, Any]], baseline: dict[str, Any] | None) -> dict[str, Any]:
    """Finished runs of one scenario, oldest first: overall numbers, each business function per run,
    and the latest run against the baseline. Uses the stored summaries only, so it stays fast."""
    rows, first_label = [], {}
    for run in runs:
        s = run["summary"]
        req = s.get("requests") or {}
        rt = req.get("response_time_ms") or {}
        sla = s.get("sla") or []
        rows.append({"id": run["id"], "created_at": run["created_at"], "status": run["status"], "verdict": s.get("verdict"),
                     "users": run["config"].get("total_users"), "requests": req.get("total_requests", 0),
                     "error_rate_percent": req.get("error_rate_percent"), "avg_ms": rt.get("avg"),
                     "p95_ms": rt.get("p95"), "throughput_rps": req.get("throughput_rps"),
                     "sla_failed": sum(1 for r in sla if r["status"] == "fail"), "sla_total": len(sla)})
        for t in s.get("transactions") or []:
            first_label.setdefault(t["display"], t["name"])
    per_run = [{t["display"]: t for t in run["summary"].get("transactions") or []} for run in runs]
    transactions = [{"name": name, "points": [{m: stats[name][m] for m in TREND_METRICS} if name in stats else None
                                              for stats in per_run]}
                    for name in sorted(first_label, key=first_label.get)]

    vs_baseline = None
    latest = runs[-1] if runs else None
    if baseline and latest and latest["id"] != baseline["id"]:
        before = {t["display"]: t for t in baseline["summary"].get("transactions") or []}
        compared = []
        for t in latest["summary"].get("transactions") or []:
            a = before.get(t["display"])
            if a:
                compared.append({"name": t["display"], "baseline_p90": a["p90"], "p90": t["p90"],
                                 "change_percent": _change(a["p90"], t["p90"]),
                                 "baseline_error_rate_percent": a["error_rate_percent"],
                                 "error_rate_percent": t["error_rate_percent"], "verdict": regression_verdict(a, t)})
        vs_baseline = {"baseline_run_id": baseline["id"], "run_id": latest["id"], "rows": compared,
                       "regressions": sum(1 for r in compared if r["verdict"] in ("slower", "more errors"))}
    return {"runs": rows, "transactions": transactions, "vs_baseline": vs_baseline}
