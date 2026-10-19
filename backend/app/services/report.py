"""Run reports (LoadRunner Analysis' summary report): one data model, rendered as HTML, PDF or Word.

The HTML report is self-contained (inline CSS and SVG charts, no scripts), so it can be mailed,
archived, or printed to PDF from any browser. report_pdf and report_docx render the same data.
"""
from __future__ import annotations

import html
import math
from datetime import datetime
from typing import Any

from . import analysis
from .result_analyzer import Sample
from .scenario_analyzer import METRIC_LABELS, label_stats, split

COLORS = ["#0f6e8c", "#b3261e", "#1f7a4d", "#a66a00", "#5b3b8c", "#16222e", "#c2185b", "#00796b"]
MAX_CHART_LINES = 6


def _bucket(duration_s: float) -> int:
    return 2 if duration_s <= 120 else 5 if duration_s <= 900 else 30 if duration_s <= 7200 else 120


def report_data(test: dict[str, Any], samples: list[Sample], monitors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Everything a report shows, for a quick test or a scenario run."""
    summary = test.get("summary") or {}
    scenario = test.get("kind") == "scenario"
    requests_summary = summary.get("requests") if scenario else summary
    requests, transactions = split(samples)
    duration = (max(s.ts + s.elapsed for s in samples) - min(s.ts for s in samples)) / 1000 if samples else 0
    bucket = _bucket(duration)
    groups = [g["name"] for g in test["config"].get("groups", [])] if scenario else []
    all_series = analysis.series(samples, bucket_seconds=bucket, group_names=groups, monitors=monitors)
    by_id = {s["id"]: s for s in all_series}
    tx_ids = [s["id"] for s in all_series if s["id"].startswith("tx:") and s["id"].endswith(":p90")]
    req_ids = [s["id"] for s in all_series if s["id"].startswith("req:")]
    charts = [
        {"title": "Running users", "unit": "users",
         "lines": [by_id[i] for i in ([f"users:{g}" for g in groups] or ["users"]) if i in by_id]},
        {"title": "Response time, 90th percentile" if tx_ids else "Average response time", "unit": "ms",
         "lines": [by_id[i] for i in (tx_ids or req_ids)][:MAX_CHART_LINES]},
        {"title": "Hits per second and errors per second", "unit": "/s",
         "lines": [by_id[i] for i in ("hits", "errors") if i in by_id]},
    ]
    if monitors:
        charts.append({"title": "Server monitors", "unit": "",
                       "lines": [s for s in all_series if s["category"] == "Servers"][:MAX_CHART_LINES]})
    rows = summary.get("transactions") if scenario else None
    if rows is None:
        rows = label_stats(transactions or requests, max(duration, 0.001)) if samples else []
    config = test["config"]
    return {
        "title": test["name"], "id": test["id"], "kind": test.get("kind", "quick"), "status": test["status"],
        "verdict": summary.get("verdict"), "error": test.get("error"),
        "started_at": test.get("started_at"), "finished_at": test.get("finished_at"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target": None if scenario else f"{config.get('method', 'GET')} {config.get('target_url', '')}",
        "groups": summary.get("groups") or [], "group_plan": config.get("groups", []),
        "requests": requests_summary or {}, "rows": rows, "rows_kind": "Business function" if (scenario or transactions) else "Request",
        "sla": summary.get("sla") or [], "goal": summary.get("goal"),
        "warnings": summary.get("warnings") or [], "errors": analysis.errors(samples),
        "charts": [c for c in charts if c["lines"]], "duration_seconds": round(duration, 1),
    }


# ---- SVG line charts ----------------------------------------------------------------------------

def nice_max(value: float) -> float:
    if value <= 0:
        return 1
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def fmt_number(v: float) -> str:
    if v >= 1000:
        return f"{v:,.0f}"
    return f"{v:g}" if v == int(v) else f"{v:.1f}"


def chart_geometry(lines: list[dict[str, Any]], width: float, height: float, left: float = 56, bottom: float = 34,
                   top: float = 10, right: float = 12) -> dict[str, Any]:
    xs = [p["t"] for line in lines for p in line["points"]] or [0]
    ys = [p["v"] for line in lines for p in line["points"] if p["v"] is not None] or [0]
    x_max = max(max(xs), 1)
    y_max = nice_max(max(ys) * 1.05)
    plot_w, plot_h = width - left - right, height - top - bottom

    def at(t: float, v: float) -> tuple[float, float]:
        return left + plot_w * t / x_max, top + plot_h * (1 - v / y_max)

    return {"x_max": x_max, "y_max": y_max, "left": left, "top": top, "plot_w": plot_w, "plot_h": plot_h, "at": at}


def svg_chart(chart: dict[str, Any], width: int = 760, height: int = 230) -> str:
    g = chart_geometry(chart["lines"], width, height)
    parts = [f'<svg viewBox="0 0 {width} {height + 24 * ((len(chart["lines"]) + 2) // 3)}" role="img" '
             f'aria-label="{html.escape(chart["title"])}" xmlns="http://www.w3.org/2000/svg" font-family="Helvetica, Arial, sans-serif" font-size="11">']
    for i in range(6):
        v = g["y_max"] * i / 5
        _, y = g["at"](0, v)
        parts.append(f'<line x1="{g["left"]}" y1="{y:.1f}" x2="{g["left"] + g["plot_w"]:.1f}" y2="{y:.1f}" stroke="#d3dae1"/>')
        parts.append(f'<text x="{g["left"] - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#5a6b7b">{fmt_number(v)}</text>')
    for i in range(7):
        t = g["x_max"] * i / 6
        x, _ = g["at"](t, 0)
        parts.append(f'<text x="{x:.1f}" y="{g["top"] + g["plot_h"] + 16:.1f}" text-anchor="middle" fill="#5a6b7b">{fmt_number(round(t))}s</text>')
    parts.append(f'<text x="12" y="{g["top"] + g["plot_h"] / 2:.1f}" transform="rotate(-90 12 {g["top"] + g["plot_h"] / 2:.1f})" '
                 f'text-anchor="middle" fill="#5a6b7b">{html.escape(chart["unit"])}</text>')
    for n, line in enumerate(chart["lines"]):
        color = COLORS[n % len(COLORS)]
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (g["at"](p["t"], p["v"] or 0) for p in line["points"]))
        if pts:
            parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>')
        lx = 12 + (n % 3) * 250
        ly = height + 8 + (n // 3) * 22
        parts.append(f'<rect x="{lx}" y="{ly - 9}" width="12" height="4" fill="{color}"/>'
                     f'<text x="{lx + 18}" y="{ly - 4}" fill="#16222e">{html.escape(line["label"][:44])}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---- HTML -----------------------------------------------------------------------------------------

def _verdict_text(d: dict[str, Any]) -> str:
    if d["status"] == "failed" and d["error"]:
        return f"The run failed: {d['error']}"
    if d["verdict"] == "pass":
        return "Passed: every limit was met." if d["sla"] or d["kind"] == "quick" else "Completed."
    if d["verdict"] == "fail":
        missed = sum(1 for r in d["sla"] if r["status"] == "fail")
        return f"Failed: {missed} SLA check{'s' if missed != 1 else ''} missed." if missed else "Failed its limits."
    return "No results."


def render_html(d: dict[str, Any]) -> str:
    e = html.escape
    r = d["requests"] or {}
    rt = r.get("response_time_ms") or {}

    def table(headers: list[str], rows: list[list[Any]], numeric_from: int = 1) -> str:
        head = "".join(f"<th>{e(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f'<td class="{"num" if i >= numeric_from else ""}">{e(str(c))}</td>'
                                        for i, c in enumerate(row)) + "</tr>" for row in rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    sections = []
    stats = [("Requests", f"{r.get('total_requests', 0):,}"), ("Errors", f"{r.get('error_rate_percent', 0)}%"),
             ("Requests/s", r.get("throughput_rps", 0)), ("p95", f"{rt.get('p95', '–')} ms"),
             ("Peak users", r.get("peak_users", 0)), ("Duration", f"{d['duration_seconds']} s")]
    sections.append('<div class="stats">' + "".join(f'<div><b>{e(str(v))}</b><span>{e(k)}</span></div>' for k, v in stats) + "</div>")
    if d["goal"]:
        g = d["goal"]
        sections.append(f'<p class="note">Goal: {g["target"]:g}/s {"requests" if g["type"] == "hits_per_second" else "business functions"}; '
                        f'reached {g["actual"]:g}/s ({"met" if g["reached"] else "not met"}).</p>')
    for chart in d["charts"]:
        sections.append(f"<h2>{e(chart['title'])}</h2><figure>{svg_chart(chart)}</figure>")
    if d["rows"]:
        sections.append(f"<h2>{e(d['rows_kind'])} summary</h2>" + table(
            [d["rows_kind"], "Count", "Passed", "Failed", "Min ms", "Avg ms", "Max ms", "90% ms", "95% ms", "Per second"],
            [[t["display"], f"{t['count']:,}", f"{t['passed']:,}", f"{t['failed']:,}", t["min"], t["avg"], t["max"],
              t["p90"], t["p95"], t["tps"]] for t in d["rows"]]))
    if d["sla"]:
        sections.append("<h2>Service level agreements</h2>" + table(
            ["Business function", "Measure", "Limit", "Actual", "Result"],
            [[s["transaction"], METRIC_LABELS[s["metric"]], s["limit"], "–" if s["actual"] is None else s["actual"],
              {"pass": "met", "fail": "MISSED", "no_data": "no data"}[s["status"]]] for s in d["sla"]], numeric_from=2))
    if d["groups"]:
        sections.append("<h2>Groups</h2>" + table(["Group", "Peak users", "Requests", "Error rate"],
                                                 [[g["name"], g["peak_users"], f"{g['requests']:,}", f"{g['error_rate_percent']}%"] for g in d["groups"]]))
    if d["errors"]:
        sections.append("<h2>Errors</h2>" + table(["Code", "Message", "Count", "First at", "Requests"],
                                                 [[x["code"], x["message"], f"{x['count']:,}", f"{x['first_seconds']} s",
                                                   ", ".join(q["label"] for q in x["requests"])] for x in d["errors"]], numeric_from=2))
    if d["warnings"]:
        sections.append("<h2>Findings</h2><ul>" + "".join(f"<li>{e(w['message'])}</li>" for w in d["warnings"]) + "</ul>")
    when = f"{d['started_at'] or ''} to {d['finished_at'] or ''}"
    subtitle = e(d["target"]) if d["target"] else e(" · ".join(f"{g['name']}: {g['users']} × {g.get('script', '')}" for g in d["group_plan"]))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(d['title'])} – load test report</title>
<style>
  body {{ font: 14px/1.5 Helvetica, Arial, sans-serif; color: #16222e; margin: 32px auto; max-width: 900px; padding: 0 16px; }}
  h1 {{ font-size: 26px; margin: 0; }} h2 {{ font-size: 17px; margin: 28px 0 8px; }}
  .sub, .muted {{ color: #5a6b7b; }} .verdict {{ font-size: 18px; margin: 8px 0; }}
  .pass {{ color: #1f7a4d; }} .fail {{ color: #b3261e; }}
  .stats {{ display: flex; flex-wrap: wrap; border-top: 1px solid #d3dae1; border-bottom: 1px solid #d3dae1; margin: 16px 0; }}
  .stats div {{ flex: 1 1 120px; padding: 10px 12px 10px 0; }} .stats b {{ display: block; font-size: 20px; }}
  .stats span {{ color: #5a6b7b; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
  th, td {{ border-bottom: 1px solid #d3dae1; padding: 5px 6px; text-align: left; vertical-align: top; }}
  th {{ color: #5a6b7b; }} td.num {{ text-align: right; white-space: nowrap; }}
  figure {{ margin: 0; }} svg {{ width: 100%; height: auto; }}
  .note {{ background: #eef1f4; padding: 8px 10px; border-radius: 6px; }}
  @media print {{ body {{ margin: 0 auto; }} h2 {{ break-after: avoid; }} figure, table {{ break-inside: avoid; }} .noprint {{ display: none; }} }}
</style></head><body>
<p class="noprint muted">Print this page (Ctrl+P) to save it as a PDF.</p>
<h1>{e(d['title'])}</h1>
<div class="sub">{subtitle}</div>
<div class="sub">{e(when)} · run {e(d['id'])} · report generated {e(d['generated_at'])}</div>
<p class="verdict {'pass' if d['verdict'] == 'pass' else 'fail'}">{e(_verdict_text(d))}</p>
{''.join(sections)}
</body></html>
"""
