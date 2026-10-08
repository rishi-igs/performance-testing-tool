"""Import HAR or JMX recordings, filter and group their requests, and export a .jmx."""
from __future__ import annotations

import re
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from ..deps import audit, require_api_key
from ..models.script import FilterRules, ScriptUpdate
from ..services import request_filter
from ..services.har_parser import ImportFailed, parse_har
from ..services.jmeter_plan_builder import build_recorded_plan
from ..services.jmx_importer import parse_jmx, rebuild

router = APIRouter(prefix="/scripts", tags=["scripts"])


def _get_or_404(request: Request, script_id: str) -> dict[str, Any]:
    script = request.app.state.db.get_script(script_id)
    if not script:
        raise HTTPException(404, f"Script '{script_id}' not found")
    return script


def _evaluate(script: dict[str, Any]) -> tuple[FilterRules, dict[str, Any]]:
    rules = FilterRules(**script["rules"])
    return rules, request_filter.evaluate(script["source"], script["items"], rules, script["overrides"])


def _detail(script: dict[str, Any]) -> dict[str, Any]:
    rules, view = _evaluate(script)
    return {
        "id": script["id"], "name": script["name"], "source": script["source"],
        "created_by": script["created_by"], "created_at": script["created_at"], "updated_at": script["updated_at"],
        "rules": rules.model_dump(), "warnings": script["warnings"], "variables": script["variables"],
        "summary": view["summary"],
        "transactions": [
            {"number": t["number"], "name": t["name"], "thread_group": t["thread_group"], "requests": len(t["items"])}
            for t in view["transactions"]
        ],
        "items": view["items"],
    }


async def _read_upload(request: Request) -> bytes:
    limit = request.app.state.settings.max_upload_bytes
    too_big = HTTPException(413, f"The file is larger than the {limit // (1024 * 1024)} MB limit (MAX_UPLOAD_MB).")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_big
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_big
        chunks.append(chunk)
    if not size:
        raise HTTPException(422, "The upload is empty. Send the file content as the request body.")
    return b"".join(chunks)


async def _import(request: Request, who: str, source: str, name: str | None,
                  parser: Callable[[bytes], dict[str, Any]]) -> dict[str, Any]:
    raw = await _read_upload(request)
    try:
        parsed = await run_in_threadpool(parser, raw)
    except ImportFailed as exc:
        raise HTTPException(422, str(exc)) from exc
    script_id = f"s_{uuid.uuid4().hex[:10]}"
    title = (name or "").strip()[:100] or str(parsed["name"]).strip()[:100] or "Imported script"
    db = request.app.state.db
    db.insert_script(
        script_id, name=title, source=source, items=parsed["items"], variables=parsed["variables"],
        warnings=parsed["warnings"], rules=FilterRules().model_dump(),
        # A JMX export rebuilds the original file, so keep it. A HAR is not kept: only the sanitized requests are.
        original=raw if source == "jmx" else None, created_by=who,
    )
    audit.info("IMPORT script=%s by=%s source=%s requests=%d", script_id, who, source, len(parsed["items"]))
    return _detail(db.get_script(script_id))


@router.post("/import-har", status_code=201)
async def import_har(request: Request, name: str | None = Query(None, max_length=100),
                     who: str = Depends(require_api_key)):
    """Upload a browser HAR file as the raw request body."""
    return await _import(request, who, "har", name, parse_har)


@router.post("/import-jmx", status_code=201)
async def import_jmx(request: Request, name: str | None = Query(None, max_length=100),
                     who: str = Depends(require_api_key)):
    """Upload a JMeter .jmx file as the raw request body."""
    return await _import(request, who, "jmx", name, parse_jmx)


@router.get("")
def list_scripts(request: Request, _: str = Depends(require_api_key)):
    return request.app.state.db.list_scripts()


@router.get("/{script_id}")
def get_script(script_id: str, request: Request, _: str = Depends(require_api_key)):
    return _detail(_get_or_404(request, script_id))


@router.put("/{script_id}/filter")
def update_filter(script_id: str, update: ScriptUpdate, request: Request, who: str = Depends(require_api_key)):
    """Save include/exclude choices and business-function names, and optionally replace the rules."""
    script = _get_or_404(request, script_id)
    for change in update.changes:
        if change.index >= script["request_count"]:
            raise HTTPException(422, f"Request {change.index} does not exist in this script")

    def mutate(rules: dict[str, Any], overrides: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if update.rules is not None:
            rules = update.rules.model_dump()
        for change in update.changes:
            key = str(change.index)
            edit = overrides.setdefault(key, {})
            if "include" in change.model_fields_set:
                if change.include is None:
                    edit.pop("include", None)
                else:
                    edit["include"] = change.include
            if "transaction" in change.model_fields_set:
                name = (change.transaction or "").strip()
                if name:
                    edit["transaction"] = name
                else:
                    edit.pop("transaction", None)
            if not edit:
                overrides.pop(key)
        return rules, overrides

    if not request.app.state.db.update_script(script_id, mutate):
        raise HTTPException(404, f"Script '{script_id}' not found")
    return _detail(_get_or_404(request, script_id))


@router.get("/{script_id}/export-jmx")
def export_jmx(
    script_id: str,
    request: Request,
    users: int = Query(1, ge=1, description="HAR imports only; a JMX import keeps its own thread groups"),
    ramp_up_seconds: int = Query(0, ge=0, le=3600),
    loops: int = Query(1, ge=1, le=100_000),
    _: str = Depends(require_api_key),
):
    settings = request.app.state.settings
    if users > settings.max_users:
        raise HTTPException(422, f"users cannot exceed {settings.max_users}")
    script = _get_or_404(request, script_id)
    _, view = _evaluate(script)
    if not view["transactions"]:
        raise HTTPException(422, "Nothing to export: every request is excluded.")
    if script["source"] == "har":
        groups = [(t["name"], [script["items"][i] for i in t["items"]]) for t in view["transactions"]]
        xml = build_recorded_plan(script["name"], groups, variables=script["variables"], users=users,
                                  ramp_up_seconds=ramp_up_seconds, loops=loops)
    else:
        xml = rebuild(script["original"], view["transactions"])
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", script["name"]).strip("-.")[:60] or script_id
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.jmx"'})


@router.delete("/{script_id}", status_code=204)
def delete_script(script_id: str, request: Request, who: str = Depends(require_api_key)):
    if not request.app.state.db.delete_script(script_id):
        raise HTTPException(404, f"Script '{script_id}' not found")
    audit.info("DELETE script=%s by=%s", script_id, who)
    return Response(status_code=204)
