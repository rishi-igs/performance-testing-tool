"""Overview of a project's testing (requirements phase 4): what runs now, alerts, recent results."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..deps import project_filter
from ..deps import viewer as require_api_key
from .tests import live_summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
ALERT_RUNS = 10


def _metrics(test: dict[str, Any], summary: dict[str, Any] | None) -> dict[str, Any]:
    """The headline numbers of a quick test or a scenario run (a scenario keeps them under "requests")."""
    if not summary:
        return {"requests": 0, "error_rate_percent": None, "p95_ms": None, "throughput_rps": None, "users": 0}
    m = summary.get("requests", summary) if test.get("kind") == "scenario" else summary
    rt = m.get("response_time_ms") or {}
    return {"requests": m.get("total_requests", 0), "error_rate_percent": m.get("error_rate_percent"),
            "p95_ms": rt.get("p95"), "throughput_rps": m.get("throughput_rps"),
            "users": m.get("active_users") if test["status"] == "running" else m.get("peak_users", 0)}


def _brief(test: dict[str, Any], summary: dict[str, Any] | None) -> dict[str, Any]:
    config = test.get("config") or {}
    return {"id": test["id"], "name": test["name"], "kind": test.get("kind", "quick"), "scenario_id": test.get("scenario_id"),
            "status": test["status"], "verdict": (summary or {}).get("verdict"), "error": test.get("error"),
            "test_type": config.get("test_type", "load") if test.get("kind") != "scenario" else None,
            "created_at": test["created_at"], "started_at": test["started_at"], "finished_at": test["finished_at"],
            "duration_seconds": config.get("duration_seconds"), **_metrics(test, summary)}


@router.get("")
def overview(request: Request, project: str | None = None, limit: int = Query(20, ge=1, le=100),
             _: str = Depends(require_api_key)):
    """Running tests with live numbers, alerts from the latest runs, and recent results with their verdicts."""
    db = request.app.state.db
    tests = db.list_tests(limit=200, projects=project_filter(request, project))
    running = [_brief(t, live_summary(request, t)) for t in tests if t["status"] == "running"]
    finished = [t for t in tests if t["status"] != "running"]

    alerts = []
    for t in finished[:ALERT_RUNS]:
        if t["status"] == "failed" and t.get("error"):
            alerts.append({"run": _ref(t), "severity": "critical", "message": t["error"]})
        for w in (t["summary"] or {}).get("warnings", []):
            if w.get("severity") == "critical":
                alerts.append({"run": _ref(t), "severity": "critical", "message": w["message"]})

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    day = [t for t in tests if t["created_at"] >= since]
    verdicts = [(t["summary"] or {}).get("verdict") for t in day if t["status"] in ("completed", "stopped")]
    return {
        "running": running,
        "alerts": alerts,
        "recent": [_brief(t, t["summary"]) for t in finished[:limit]],
        "last_day": {"runs": len(day), "passed": verdicts.count("pass"), "failed": verdicts.count("fail")
                     + sum(1 for t in day if t["status"] == "failed"), "running": len(running)},
        "scripts": len(db.list_scripts(projects=project_filter(request, project))),
        "scenarios": len(db.list_scenarios(projects=project_filter(request, project))),
    }


def _ref(t: dict[str, Any]) -> dict[str, Any]:
    return {"id": t["id"], "name": t["name"], "kind": t.get("kind", "quick"), "scenario_id": t.get("scenario_id"),
            "finished_at": t["finished_at"]}
