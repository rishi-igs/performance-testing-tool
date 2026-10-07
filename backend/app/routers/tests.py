"""Create, list, inspect and stop performance tests."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import audit, require_api_key
from ..models.test_config import LoadTestConfig
from ..models.test_result import TestCreated, TestRecord
from ..services import result_analyzer
from ..services.security import TargetNotAllowed, check_target
from ..services.test_executor import TooManyTests, run_paths

router = APIRouter(prefix="/tests", tags=["tests"])


def get_test_or_404(request: Request, test_id: str) -> dict[str, Any]:
    test = request.app.state.db.get_test(test_id)
    if not test:
        raise HTTPException(404, f"Test '{test_id}' not found")
    return test


def live_summary(request: Request, test: dict[str, Any]) -> dict[str, Any] | None:
    """Stored summary for finished tests; computed from the partial results file for running ones."""
    if test["summary"] is not None:
        return test["summary"]
    from ..models.test_config import Thresholds
    paths = run_paths(request.app.state.settings, test["id"])
    thresholds = Thresholds(**test["config"].get("thresholds", {}))
    return result_analyzer.summarize(result_analyzer.parse_jtl(paths["jtl"]), thresholds)


@router.post("", status_code=201, response_model=TestCreated)
def create_test(config: LoadTestConfig, request: Request, who: str = Depends(require_api_key)):
    settings = request.app.state.settings
    if config.users > settings.max_users:
        raise HTTPException(422, f"users cannot exceed {settings.max_users}")
    if config.duration_seconds > settings.max_duration_seconds:
        raise HTTPException(422, f"duration_seconds cannot exceed {settings.max_duration_seconds}")
    try:
        check_target(config.target_url, settings)
    except TargetNotAllowed as exc:
        audit.warning("REJECTED target by=%s url=%s reason=%s", who, config.target_url, exc)
        raise HTTPException(422, str(exc)) from exc

    test_id = f"t_{uuid.uuid4().hex[:10]}"
    db = request.app.state.db
    # Store the masked config: secrets in headers must not be persisted in the database.
    db.insert_test(test_id, config.name, config.masked(), who)
    try:
        request.app.state.executor.start(test_id, config)
    except TooManyTests as exc:
        db.update_test(test_id, status="failed", error=str(exc))
        raise HTTPException(429, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        db.update_test(test_id, status="failed", error=f"Could not start test: {exc}")
        raise HTTPException(500, f"Could not start test: {exc}") from exc
    audit.info("START test=%s by=%s url=%s users=%s duration=%ss", test_id, who, config.target_url, config.users, config.duration_seconds)
    return TestCreated(id=test_id, status="running")


@router.get("", response_model=list[TestRecord])
def list_tests(request: Request, _: str = Depends(require_api_key)):
    return request.app.state.db.list_tests()


@router.get("/{test_id}", response_model=TestRecord)
def get_test(test_id: str, request: Request, _: str = Depends(require_api_key)):
    test = get_test_or_404(request, test_id)
    test["summary"] = live_summary(request, test)
    return test


@router.get("/{test_id}/status")
def get_status(test_id: str, request: Request, _: str = Depends(require_api_key)):
    test = get_test_or_404(request, test_id)
    return {
        "id": test["id"],
        "status": test["status"],
        "error": test["error"],
        "started_at": test["started_at"],
        "finished_at": test["finished_at"],
        "metrics": live_summary(request, test),
    }


@router.get("/{test_id}/timeline")
def get_timeline(test_id: str, request: Request, bucket_seconds: int = 5, _: str = Depends(require_api_key)):
    test = get_test_or_404(request, test_id)
    if not 1 <= bucket_seconds <= 600:
        raise HTTPException(422, "bucket_seconds must be between 1 and 600")
    paths = run_paths(request.app.state.settings, test["id"])
    samples = result_analyzer.parse_jtl(paths["jtl"])
    return {"bucket_seconds": bucket_seconds, "points": result_analyzer.timeline(samples, bucket_seconds)}


@router.post("/{test_id}/stop")
def stop_test(test_id: str, request: Request, who: str = Depends(require_api_key)):
    test = get_test_or_404(request, test_id)
    if test["status"] != "running":
        raise HTTPException(409, f"Test is already {test['status']}")
    if not request.app.state.executor.stop(test_id):
        raise HTTPException(409, "Test is not running on this server")
    audit.info("STOP test=%s by=%s", test_id, who)
    return {"id": test_id, "status": "stopping"}
