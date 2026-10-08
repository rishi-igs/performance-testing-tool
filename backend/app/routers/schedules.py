"""Scheduled runs of scenarios (LoadRunner Enterprise's timeslots, simplified)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..deps import audit, ensure_project, project_filter, tester
from ..deps import viewer as require_api_key
from ..models.schedule import ScheduleIn
from ..services.scheduler import iso, next_run, zone_of, zone_problem

router = APIRouter(prefix="/schedules", tags=["schedules"])


def _view(schedule: dict[str, Any]) -> dict[str, Any]:
    return schedule | {"zone_mode": zone_of(schedule["config"])[1]}


def _get(request: Request, schedule_id: str) -> dict[str, Any]:
    schedule = request.app.state.db.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")
    ensure_project(request, schedule["project_id"])
    return schedule


def _scenario(request: Request, scenario_id: str) -> dict[str, Any]:
    scenario = request.app.state.db.get_scenario(scenario_id)
    if not scenario:
        raise HTTPException(422, f"Scenario '{scenario_id}' not found")
    ensure_project(request, scenario["project_id"])
    return scenario


def _first_run(cfg: dict[str, Any]) -> str | None:
    problem = zone_problem(cfg)
    if problem:
        raise HTTPException(422, problem)
    if not cfg["enabled"]:
        return None
    first = next_run(cfg, datetime.now(timezone.utc))
    if first is None:
        raise HTTPException(422, "That date and time has already passed")
    return iso(first)


@router.get("")
def list_schedules(request: Request, scenario_id: str | None = None, project: str | None = None,
                   _: str = Depends(require_api_key)):
    db = request.app.state.db
    return [_view(s) for s in db.list_schedules(projects=project_filter(request, project), scenario_id=scenario_id)]


@router.post("", status_code=201)
def create_schedule(body: ScheduleIn, request: Request, who: str = Depends(tester)):
    """Runs pass the same limits and target checks as a run started by hand."""
    scenario = _scenario(request, body.scenario_id)
    cfg = body.model_dump()
    first = _first_run(cfg)
    schedule_id = f"h_{uuid.uuid4().hex[:10]}"
    db = request.app.state.db
    db.insert_schedule(schedule_id, scenario["project_id"], scenario["id"], cfg, first, who)
    audit.info("SCHEDULE create=%s scenario=%s repeat=%s next=%s by=%s", schedule_id, scenario["id"], body.repeat, first, who)
    return _view(db.get_schedule(schedule_id))


@router.put("/{schedule_id}")
def update_schedule(schedule_id: str, body: ScheduleIn, request: Request, who: str = Depends(tester)):
    existing = _get(request, schedule_id)
    if body.scenario_id != existing["scenario_id"]:
        raise HTTPException(422, "A schedule belongs to one scenario; create a new schedule for another")
    cfg = body.model_dump()
    first = _first_run(cfg)
    db = request.app.state.db
    db.update_schedule(schedule_id, config_json=json.dumps(cfg), next_run_at=first, last_error=None)
    audit.info("SCHEDULE update=%s next=%s by=%s", schedule_id, first, who)
    return _view(db.get_schedule(schedule_id))


@router.delete("/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: str, request: Request, who: str = Depends(tester)):
    _get(request, schedule_id)
    request.app.state.db.delete_schedule(schedule_id)
    audit.info("SCHEDULE delete=%s by=%s", schedule_id, who)
    return Response(status_code=204)
