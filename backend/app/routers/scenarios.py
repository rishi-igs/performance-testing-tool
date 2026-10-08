"""Scenarios: groups of virtual users running scripts on a schedule (LoadRunner's Controller)."""
from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from ..db import DEFAULT_PROJECT, Database, now_iso
from ..deps import audit, ensure_project, project_filter, target_project, tester, visible_projects
from ..deps import viewer as require_api_key
from ..models.scenario import ScenarioConfig
from ..services import analysis, result_analyzer, scenario_analyzer
from ..services.monitoring import MonitorSampler, monitor_errors, monitor_series, monitor_warnings
from ..services.scenario_builder import ScenarioError, build_scenario, planned_users
from ..services.test_executor import Outcome, TooManyTests, finish_status, run_paths
from .debug import check_hosts
from .infra import check_generator

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


def get_scenario_or_404(request: Request, scenario_id: str) -> dict[str, Any]:
    scenario = request.app.state.db.get_scenario(scenario_id)
    if not scenario:
        raise HTTPException(404, f"Scenario '{scenario_id}' not found")
    ensure_project(request, scenario.get("project_id"))
    return scenario


def _check_scripts(db: Database, cfg: ScenarioConfig, project_id: str) -> None:
    """Groups may only run scripts of the scenario's own project."""
    for group in cfg.groups:
        script = db.get_script(group.script_id)
        if not script or script.get("project_id", DEFAULT_PROJECT) != project_id:
            raise HTTPException(422, f"Group {group.name}: script {group.script_id} does not exist in this project")


def _project_for(request: Request, cfg: ScenarioConfig, project: str | None) -> str:
    """The project asked for; else the one the scripts are in; else the caller's default project."""
    if project:
        return target_project(request, project)
    db = request.app.state.db
    found = {(db.get_script(g.script_id) or {}).get("project_id") for g in cfg.groups} - {None}
    seen = visible_projects(request)
    if len(found) == 1 and (seen is None or found <= seen):
        return found.pop()
    return target_project(request, None)


def _check_limits(settings: Any, cfg: ScenarioConfig) -> None:
    if cfg.total_users_planned() > settings.max_users:
        raise HTTPException(422, f"The scenario needs {cfg.total_users_planned()} users; the limit is "
                                 f"{settings.max_users} (MAX_USERS)")
    if cfg.total_seconds() > settings.max_duration_seconds:
        raise HTTPException(422, f"The scenario lasts {cfg.total_seconds()} s; the limit is "
                                 f"{settings.max_duration_seconds} s (MAX_DURATION_SECONDS)")


def plan_for(db: Database, cfg: ScenarioConfig, *, engines: int = 1):
    ids = {g.script_id for g in cfg.active_groups()}
    scripts = {sid: db.get_script(sid) for sid in ids}
    scripts = {sid: s for sid, s in scripts.items() if s}
    try:
        return build_scenario(cfg, scripts, {sid: db.script_files(sid) for sid in scripts}, engines=engines)
    except ScenarioError as exc:
        raise HTTPException(422, str(exc)) from exc


def _detail(db: Database, scenario: dict[str, Any]) -> dict[str, Any]:
    cfg = ScenarioConfig(**scenario["config"])
    runs = db.list_tests(limit=20, kind="scenario", scenario_id=scenario["id"])
    return {**scenario, "config": cfg.model_dump(), "total_users": cfg.total_users_planned(),
            "total_seconds": cfg.total_seconds(),
            "runs": [{k: r[k] for k in ("id", "status", "created_at", "finished_at", "error")} |
                     {"verdict": (r["summary"] or {}).get("verdict")} for r in runs]}


@router.post("", status_code=201)
def create_scenario(cfg: ScenarioConfig, request: Request, project: str | None = None, who: str = Depends(tester)):
    db = request.app.state.db
    project_id = _project_for(request, cfg, project)
    _check_scripts(db, cfg, project_id)
    scenario_id = f"c_{uuid.uuid4().hex[:10]}"
    db.insert_scenario(scenario_id, cfg.model_dump(), who, project_id)
    audit.info("SCENARIO create=%s project=%s by=%s", scenario_id, project_id, who)
    return _detail(db, db.get_scenario(scenario_id))


@router.get("")
def list_scenarios(request: Request, project: str | None = None, _: str = Depends(require_api_key)):
    return request.app.state.db.list_scenarios(projects=project_filter(request, project))


@router.get("/{scenario_id}")
def get_scenario(scenario_id: str, request: Request, _: str = Depends(require_api_key)):
    return _detail(request.app.state.db, get_scenario_or_404(request, scenario_id))


@router.put("/{scenario_id}")
def update_scenario(scenario_id: str, cfg: ScenarioConfig, request: Request, who: str = Depends(tester)):
    scenario = get_scenario_or_404(request, scenario_id)
    db = request.app.state.db
    _check_scripts(db, cfg, scenario["project_id"])
    db.update_scenario(scenario_id, cfg.model_dump())
    audit.info("SCENARIO update=%s by=%s", scenario_id, who)
    return _detail(db, db.get_scenario(scenario_id))


@router.delete("/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: str, request: Request, who: str = Depends(tester)):
    get_scenario_or_404(request, scenario_id)
    request.app.state.db.delete_scenario(scenario_id)
    audit.info("SCENARIO delete=%s by=%s", scenario_id, who)
    return Response(status_code=204)


@router.get("/{scenario_id}/preview")
def preview(scenario_id: str, request: Request, _: str = Depends(require_api_key)):
    """Planned running users over time per group, and what the plan will contain."""
    cfg = ScenarioConfig(**get_scenario_or_404(request, scenario_id)["config"])
    plan = plan_for(request.app.state.db, cfg)
    return {**planned_users(cfg), "groups_plan": plan.groups, "warnings": plan.warnings, "hosts": plan.hosts,
            "total_users": plan.total_users}


@router.get("/{scenario_id}/export-jmx")
def export(scenario_id: str, request: Request, _: str = Depends(require_api_key)):
    scenario = get_scenario_or_404(request, scenario_id)
    plan = plan_for(request.app.state.db, ScenarioConfig(**scenario["config"]))
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", scenario["name"]).strip("-.")[:60] or scenario_id
    if not plan.files:
        return Response(plan.xml, media_type="application/xml",
                        headers={"Content-Disposition": f'attachment; filename="{name}.jmx"'})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{name}.jmx", plan.xml)
        for path, content in plan.files.items():
            z.writestr(path, content)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


# ---- trends and the baseline ------------------------------------------------------------------

def _has_results(run: dict[str, Any] | None) -> bool:
    return bool(run and run["status"] in ("completed", "stopped") and run["summary"]
                and run["summary"].get("verdict") != "no_data")


@router.get("/{scenario_id}/trends")
def trends(scenario_id: str, request: Request, limit: int = Query(30, ge=2, le=200), _: str = Depends(require_api_key)):
    """How the scenario's results changed from run to run, and the latest run against the baseline."""
    scenario = get_scenario_or_404(request, scenario_id)
    db = request.app.state.db
    runs = [r for r in db.list_tests(limit=limit, kind="scenario", scenario_id=scenario_id) if _has_results(r)]
    runs.reverse()
    baseline_id = scenario.get("baseline_run_id")
    baseline = next((r for r in runs if r["id"] == baseline_id), None)
    if baseline_id and baseline is None:
        found = db.get_test(baseline_id)
        baseline = found if _has_results(found) else None
    return {"scenario_id": scenario_id, "baseline_run_id": baseline["id"] if baseline else None,
            **analysis.trends(runs, baseline)}


class BaselineIn(BaseModel):
    run_id: str | None = None


@router.put("/{scenario_id}/baseline")
def set_baseline(scenario_id: str, body: BaselineIn, request: Request, who: str = Depends(tester)):
    """The run later runs are compared with; null clears it."""
    get_scenario_or_404(request, scenario_id)
    db = request.app.state.db
    if body.run_id:
        run = db.get_test(body.run_id)
        if not run or run.get("scenario_id") != scenario_id:
            raise HTTPException(422, f"Run {body.run_id} is not a run of this scenario")
        if not _has_results(run):
            raise HTTPException(422, "Only a finished run with results can be the baseline")
    db.set_baseline(scenario_id, body.run_id)
    audit.info("BASELINE scenario=%s run=%s by=%s", scenario_id, body.run_id, who)
    return {"scenario_id": scenario_id, "baseline_run_id": body.run_id}


# ---- running ------------------------------------------------------------------------------------

def _resources(db: Database, table: str, ids: list[str]) -> list[dict[str, Any]]:
    out = []
    for item_id in ids:
        item = db.get_resource(table, item_id)
        if not item:
            raise HTTPException(422, f"{table[:-1].capitalize()} {item_id} no longer exists")
        if item["config"].get("enabled", True):
            out.append(item)
    return out


def start_scenario_run(state: Any, scenario: dict[str, Any], who: str) -> dict[str, Any]:
    """Build and start a run. `state` is the application state, so the API and the scheduler share this."""
    settings, db = state.settings, state.db
    cfg = ScenarioConfig(**scenario["config"])
    _check_limits(settings, cfg)
    generators = _resources(db, "generators", cfg.generators)
    for g in generators:
        status = check_generator(g["config"]["host"], g["config"]["port"])
        db.update_resource("generators", g["id"], status=status)
        if not status["ok"]:
            raise HTTPException(422, f"Load generator {g['config']['name']}: {status['error']}")
    monitors = _resources(db, "monitors", cfg.monitors)
    plan = plan_for(db, cfg, engines=max(1, len(generators)))
    check_hosts(settings, plan.hosts, who)

    properties = list(plan.properties)
    if generators:
        properties += ["-R", ",".join(f"{g['config']['host']}:{g['config']['port']}" for g in generators)]
        if settings.rmi_ssl_disable:
            properties.append("-Jserver.rmi.ssl.disable=true")
        if settings.generator_data_dir:
            properties.append(f"-Gperf.data_dir={settings.generator_data_dir}")
    run_id = f"t_{uuid.uuid4().hex[:10]}"
    config = {"kind": "scenario", "scenario_id": scenario["id"], "scenario": cfg.model_dump(), "groups": plan.groups,
              "duration_seconds": plan.duration_seconds, "total_users": plan.total_users,
              "warnings": plan.warnings, "hosts": plan.hosts,
              "generators": [{"name": g["config"]["name"], "host": g["config"]["host"]} for g in generators],
              "monitors": [m["config"]["name"] for m in monitors]}
    db.insert_test(run_id, cfg.name, config, who, kind="scenario", scenario_id=scenario["id"],
                   project_id=scenario.get("project_id") or DEFAULT_PROJECT)
    names = [g["name"] for g in plan.groups]
    sampler: list[MonitorSampler] = []

    def finish(rid: str, outcome: Outcome) -> None:
        if sampler:
            sampler[0].finish()
        samples = result_analyzer.parse_jtl(outcome.paths["jtl"])
        summary = scenario_analyzer.scenario_summary(samples, cfg, names)
        if monitors:
            series = monitor_series(outcome.paths["dir"])
            summary["warnings"] += monitor_warnings(series, min((s.ts for s in samples), default=None))
            summary["monitor_errors"] = monitor_errors(outcome.paths["dir"])
        status, error = finish_status(outcome, has_results=bool(samples))
        db.update_test(rid, status=status, exit_code=outcome.exit_code, error=error, finished_at=now_iso(),
                       summary_json=json.dumps(summary))

    duration = plan.duration_seconds if not any(cfg.schedule_for(g).iterations for g in cfg.active_groups()) \
        else settings.max_duration_seconds
    try:
        state.executor.launch(run_id, plan.xml, duration_seconds=duration, files=plan.files,
                              properties=tuple(properties), finish=finish,
                              on_start=lambda rid: db.update_test(rid, started_at=now_iso()))
    except TooManyTests as exc:
        db.update_test(run_id, status="failed", error=str(exc), finished_at=now_iso())
        raise HTTPException(429, str(exc)) from exc
    if monitors:
        sampler.append(MonitorSampler(run_paths(settings, run_id)["dir"], monitors,
                                      lambda: state.executor.is_running(run_id)))
        sampler[0].start()
    audit.info("START scenario=%s run=%s by=%s users=%d duration=%ss generators=%d monitors=%d", scenario["id"], run_id,
               who, plan.total_users, plan.duration_seconds, len(generators), len(monitors))
    return {"id": run_id, "status": "running", "warnings": plan.warnings}


@router.post("/{scenario_id}/run", status_code=201)
def run_scenario(scenario_id: str, request: Request, who: str = Depends(tester)):
    return start_scenario_run(request.app.state, get_scenario_or_404(request, scenario_id), who)
