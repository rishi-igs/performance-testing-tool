"""Download results, exports and the JMeter HTML report for a test."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from ..deps import require_api_key
from ..services.test_executor import run_paths
from .tests import get_test_or_404, live_summary

router = APIRouter(prefix="/reports", tags=["reports"])


def _file(request: Request, test_id: str, key: str, filename: str, media_type: str) -> FileResponse:
    get_test_or_404(request, test_id)
    path = run_paths(request.app.state.settings, test_id)[key]
    if not path.is_file():
        raise HTTPException(404, f"{filename} is not available for this test yet")
    return FileResponse(path, media_type=media_type, filename=filename)


@router.get("/{test_id}")
def report_index(test_id: str, request: Request, _: str = Depends(require_api_key)):
    get_test_or_404(request, test_id)
    index = run_paths(request.app.state.settings, test_id)["report"] / "index.html"
    if not index.is_file():
        raise HTTPException(404, "The HTML report is generated when the test finishes")
    return RedirectResponse(f"/reports/{test_id}/html/index.html")


@router.get("/{test_id}/html/{asset:path}")
def report_asset(test_id: str, asset: str, request: Request, _: str = Depends(require_api_key)):
    get_test_or_404(request, test_id)
    root = run_paths(request.app.state.settings, test_id)["report"].resolve()
    target = (root / asset).resolve()
    if root not in target.parents and target != root:      # block path traversal
        raise HTTPException(404, "Not found")
    if not target.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(target)


@router.get("/{test_id}/metrics.json")
def metrics_json(test_id: str, request: Request, _: str = Depends(require_api_key)):
    test = get_test_or_404(request, test_id)
    body = {
        "id": test["id"], "name": test["name"], "status": test["status"],
        "config": test["config"], "started_at": test["started_at"],
        "finished_at": test["finished_at"], "metrics": live_summary(request, test),
    }
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{test_id}-metrics.json"'})


@router.get("/{test_id}/results.csv")
def results_csv(test_id: str, request: Request, _: str = Depends(require_api_key)):
    return _file(request, test_id, "jtl", f"{test_id}-results.csv", "text/csv")


@router.get("/{test_id}/plan.jmx")
def plan_jmx(test_id: str, request: Request, _: str = Depends(require_api_key)):
    return _file(request, test_id, "plan", f"{test_id}.jmx", "application/xml")


@router.get("/{test_id}/jmeter.log")
def jmeter_log(test_id: str, request: Request, _: str = Depends(require_api_key)):
    get_test_or_404(request, test_id)
    paths = run_paths(request.app.state.settings, test_id)
    for key in ("log", "stdout"):
        if paths[key].is_file() and paths[key].stat().st_size:
            return FileResponse(paths[key], media_type="text/plain", filename=f"{test_id}-{paths[key].name}")
    raise HTTPException(404, "No log available")
