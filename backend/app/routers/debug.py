"""Debug-run history, request/response samples and diagnosis."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..deps import audit, require_api_key
from ..models.script import DebugRunRequest
from ..services.debug_runner import debug_paths, parse_debug_samples
from ..services.failure_diagnosis import diagnose
from ..services.security import TargetNotAllowed, check_target
from ..services.test_executor import TooManyTests
from .scripts import _evaluate, _get_or_404

router = APIRouter(prefix="/debug-runs", tags=["debug runs"])


def _run_or_404(request: Request, run_id: str) -> dict[str, Any]:
    run = request.app.state.db.get_debug_run(run_id)
    if not run:
        raise HTTPException(404, f"Debug run '{run_id}' not found")
    return run


def _samples(request: Request, run: dict[str, Any]) -> list[dict[str, Any]]:
    script = request.app.state.db.get_script(run["script_id"])
    if not script:
        raise HTTPException(404, f"Script '{run['script_id']}' not found")
    return parse_debug_samples(debug_paths(request.app.state.settings, run["id"])["jtl"], script)


@router.get("/{run_id}")
def get_debug_run(run_id: str, request: Request, _: str = Depends(require_api_key)):
    return _run_or_404(request, run_id)


@router.get("/{run_id}/samples")
def list_samples(
    run_id: str,
    request: Request,
    failed_only: bool = False,
    search: str = Query("", max_length=200),
    _: str = Depends(require_api_key),
):
    run = _run_or_404(request, run_id)
    samples = _samples(request, run)
    if failed_only:
        samples = [sample for sample in samples if not sample["success"]]
    if search:
        needle = search.casefold()
        samples = [sample for sample in samples if needle in json.dumps(sample, ensure_ascii=False).casefold()]
    return {"run": run, "samples": samples}


@router.get("/{run_id}/samples/{sample_index}")
def get_sample(run_id: str, sample_index: int, request: Request, _: str = Depends(require_api_key)):
    run = _run_or_404(request, run_id)
    samples = _samples(request, run)
    if not 0 <= sample_index < len(samples):
        raise HTTPException(404, f"Sample {sample_index} not found in debug run '{run_id}'")
    return samples[sample_index]


@router.get("/{run_id}/diagnosis")
def get_diagnosis(run_id: str, request: Request, _: str = Depends(require_api_key)):
    run = _run_or_404(request, run_id)
    if run["status"] == "running":
        raise HTTPException(409, "Diagnosis is available after the debug run finishes.")
    return diagnose(_samples(request, run))


@router.get("/{run_id}/results.jtl")
def debug_results_jtl(run_id: str, request: Request, _: str = Depends(require_api_key)):
    _run_or_404(request, run_id)
    path = debug_paths(request.app.state.settings, run_id)["jtl"]
    if not path.is_file():
        raise HTTPException(404, "Raw debug JTL is not available yet")
    return FileResponse(path, media_type="application/xml", filename=f"{run_id}-results.jtl")


@router.get("/{run_id}/jmeter.log")
def debug_jmeter_log(run_id: str, request: Request, _: str = Depends(require_api_key)):
    _run_or_404(request, run_id)
    paths = debug_paths(request.app.state.settings, run_id)
    path = paths["log"] if paths["log"].is_file() and paths["log"].stat().st_size else paths["stdout"]
    if not path.is_file():
        raise HTTPException(404, "Debug JMeter log is not available yet")
    return FileResponse(path, media_type="text/plain", filename=f"{run_id}-{path.name}")


@router.post("/{run_id}/rerun", status_code=202)
async def rerun_debug(
    run_id: str,
    request: Request,
    _: str = Depends(require_api_key),
):
    previous = _run_or_404(request, run_id)
    return await _start_debug(previous["script_id"], request, DebugRunRequest(
        users=previous["users"], loops=previous["loops"],
    ), "rerun")


async def _start_debug(script_id: str, request: Request, config: DebugRunRequest, who: str):
    script = _get_or_404(request, script_id)
    if script["source"] != "har":
        raise HTTPException(422, "Debug runs are currently available for HAR imports only.")
    _, view = _evaluate(script)
    if not view["transactions"]:
        raise HTTPException(422, "Keep at least one request before starting a debug run.")
    for transaction in view["transactions"]:
        for index in transaction["items"]:
            try:
                check_target(script["items"][index]["url"], request.app.state.settings)
            except TargetNotAllowed as exc:
                raise HTTPException(422, f"Debug target rejected: {exc}") from exc

    from ..services.har_validator import validate_har
    validation = validate_har(
        name=script["name"], items=script["items"], transactions=view["transactions"],
        correlations=script["correlations"], variables=script["variables"],
        parameterization=script["parameterization"], csv_headers=script["csv_headers"],
        assertions=script["assertions"],
    )
    if validation["status"] == "Fail":
        raise HTTPException(422, detail={
            "message": "Fix blocking HAR validation issues before starting a debug run.",
            "validation": validation,
        })

    parameterization = script["parameterization"]
    csv_data = None
    if parameterization.get("parameters"):
        from ..services.parameterization import inspect_csv
        from .scripts import _read_upload
        csv_data = await _read_upload(request)
        try:
            headers, _ = inspect_csv(csv_data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if headers != script["csv_headers"]:
            raise HTTPException(422, "Debug CSV headers must match the validated CSV headers.")

    run_id = f"d_{uuid.uuid4().hex[:10]}"
    from ..services.jmeter_plan_builder import build_recorded_plan
    groups = [
        (transaction["name"], [script["items"][index] for index in transaction["items"]])
        for transaction in view["transactions"]
    ]
    plan = build_recorded_plan(
        script["name"] + " debug", groups, variables=script["variables"],
        users=config.users, loops=config.loops, correlations=script["correlations"],
        parameterization=parameterization, csv_headers=script["csv_headers"],
        assertions=script["assertions"],
    )
    db = request.app.state.db
    db.insert_debug_run(run_id, script_id, config.users, config.loops)
    try:
        request.app.state.debug_executor.start(run_id, script, plan, csv_data)
    except Exception as exc:
        db.update_debug_run(run_id, status="failed", error=f"Could not start debug run: {exc}",
                            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if isinstance(exc, TooManyTests):
            raise HTTPException(429, str(exc)) from exc
        raise HTTPException(500, f"Could not start debug run: {exc}") from exc
    audit.info("DEBUG RUN script=%s run=%s by=%s users=%s loops=%s",
               script_id, run_id, who, config.users, config.loops)
    return {"id": run_id, "script_id": script_id, "status": "running"}


@router.get("/scripts/{script_id}")
def list_script_debug_runs(script_id: str, request: Request, _: str = Depends(require_api_key)):
    _get_or_404(request, script_id)
    return request.app.state.db.list_debug_runs(script_id)


@router.post("/scripts/{script_id}", status_code=202)
async def start_debug(
    script_id: str,
    request: Request,
    users: int = Query(1, ge=1, le=5),
    loops: int = Query(1, ge=1, le=5),
    who: str = Depends(require_api_key),
):
    return await _start_debug(script_id, request, DebugRunRequest(users=users, loops=loops), who)
