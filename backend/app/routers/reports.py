"""Download results, exports and the JMeter HTML report for a test."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from ..deps import require_api_key
from ..services import report, report_docx, report_pdf, result_analyzer
from ..services.monitoring import monitor_series
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


def _report(request: Request, test_id: str) -> dict:
    test = get_test_or_404(request, test_id)
    test["summary"] = live_summary(request, test)
    paths = run_paths(request.app.state.settings, test_id)
    return report.report_data(test, result_analyzer.parse_jtl(paths["jtl"]), monitor_series(paths["dir"]))


def _filename(data: dict, ext: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", data["title"]).strip("-.")[:50] or "report"
    return f'attachment; filename="{name}-{data["id"]}.{ext}"'


@router.get("/{test_id}/summary.html")
def summary_html(test_id: str, request: Request, download: bool = False, _: str = Depends(require_api_key)):
    """A self-contained report (inline charts); print it to save a PDF."""
    data = _report(request, test_id)
    headers = {"Content-Disposition": _filename(data, "html")} if download else {}
    return HTMLResponse(report.render_html(data), headers=headers)


@router.get("/{test_id}/summary.pdf")
def summary_pdf(test_id: str, request: Request, _: str = Depends(require_api_key)):
    data = _report(request, test_id)
    return Response(report_pdf.render_pdf(data), media_type="application/pdf",
                    headers={"Content-Disposition": _filename(data, "pdf")})


@router.get("/{test_id}/summary.docx")
def summary_docx(test_id: str, request: Request, _: str = Depends(require_api_key)):
    data = _report(request, test_id)
    return Response(report_docx.render_docx(data),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": _filename(data, "docx")})


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
