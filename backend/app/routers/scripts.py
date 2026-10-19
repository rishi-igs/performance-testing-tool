"""Import HAR or JMX recordings, filter and group their requests, and export a .jmx."""
from __future__ import annotations

import re
import shutil
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from ..deps import audit, require_api_key
from ..models.script import AssertionsUpdate, CorrelationsUpdate, FilterRules, ParameterizationUpdate, ScriptUpdate
from ..services import request_filter
from ..services.har_parser import ImportFailed, parse_har
from ..services.jmeter_plan_builder import build_recorded_plan
from ..services.har_validator import validate_har
from ..services.jmx_importer import parse_jmx, rebuild
from ..services.parameterization import apply_parameters, inspect_csv
from ..services.debug_runner import debug_paths

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
        "correlations": script["correlations"],
        "assertions": script["assertions"],
        "assertion_warnings": [
            f"Request {item['index'] + 1} has no assertion."
            for item in view["items"]
            if item["include"] and not _has_assertion(script["assertions"], item["index"], item["transaction"])
        ],
        "parameterization": {
            **script["parameterization"],
            "csv_headers": script["csv_headers"],
            "csv_row_count": script["csv_row_count"],
        },
        "summary": view["summary"],
        "transactions": [
            {"number": t["number"], "name": t["name"], "thread_group": t["thread_group"], "requests": len(t["items"])}
            for t in view["transactions"]
        ],
        "items": view["items"],
    }


def _has_assertion(assertions: list[dict[str, Any]], index: int, transaction: str) -> bool:
    return any(
        rule["checks"] and (
            rule["scope"] == "global"
            or (rule["scope"] == "request" and rule["request_index"] == index)
            or (rule["scope"] == "transaction" and rule["transaction"] == transaction)
        )
        for rule in assertions
    )


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
    assertions = parsed.get("assertions", []) if source == "har" else []
    if source == "har" and not assertions:
        assertions = [
            {"scope": "request", "request_index": item["index"], "checks": [
                {"type": "status_code", "expected": item["status"]}
            ]}
            for item in parsed["items"] if item.get("status")
        ]
    db = request.app.state.db
    db.insert_script(
        script_id, name=title, source=source, items=parsed["items"], variables=parsed["variables"],
        warnings=parsed["warnings"], rules=FilterRules().model_dump(),
        correlations=parsed.get("correlations", []),
        assertions=assertions,
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


@router.put("/{script_id}/assertions")
def update_assertions(
    script_id: str,
    update: AssertionsUpdate,
    request: Request,
    who: str = Depends(require_api_key),
):
    script = _get_or_404(request, script_id)
    if script["source"] != "har":
        raise HTTPException(422, "Assertions can only be edited for HAR imports.")
    names = {item["index"]: item["transaction"] for item in _evaluate(script)[1]["items"]}
    for rule in update.assertions:
        if rule.scope == "request" and rule.request_index not in names:
            raise HTTPException(422, f"Request {rule.request_index} does not exist in this script.")
        if rule.scope == "transaction" and rule.transaction not in set(names.values()):
            raise HTTPException(422, f"Transaction '{rule.transaction}' does not exist in this script.")
    if not request.app.state.db.update_assertions(script_id, [rule.model_dump() for rule in update.assertions]):
        raise HTTPException(404, f"Script '{script_id}' not found")
    audit.info("ASSERTIONS script=%s by=%s rules=%d", script_id, who, len(update.assertions))
    return _detail(_get_or_404(request, script_id))


@router.get("/{script_id}/correlations")
def get_correlations(script_id: str, request: Request, _: str = Depends(require_api_key)):
    return _get_or_404(request, script_id)["correlations"]


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


@router.put("/{script_id}/correlations")
def update_correlations(
    script_id: str,
    update: CorrelationsUpdate,
    request: Request,
    who: str = Depends(require_api_key),
):
    script = _get_or_404(request, script_id)
    if script["source"] != "har":
        raise HTTPException(422, "Correlation rules can only be edited for HAR imports.")
    for rule in update.correlations:
        indexes = [rule.source_index, *rule.target_indices]
        if any(index >= script["request_count"] for index in indexes):
            raise HTTPException(422, "A correlation source or target request does not exist in this script.")
        if rule.source_index >= min(rule.target_indices):
            raise HTTPException(422, "A correlation source must come before all of its target requests.")
    correlations = [rule.model_dump() for rule in update.correlations]
    if not request.app.state.db.update_correlations(script_id, correlations):
        raise HTTPException(404, f"Script '{script_id}' not found")
    audit.info("CORRELATIONS script=%s by=%s rules=%d", script_id, who, len(correlations))
    return _detail(_get_or_404(request, script_id))


@router.put("/{script_id}/parameters/csv")
async def upload_parameter_csv(
    script_id: str,
    request: Request,
    who: str = Depends(require_api_key),
):
    """Validate CSV metadata only; parameter data is never stored on the server."""
    script = _get_or_404(request, script_id)
    if script["source"] != "har":
        raise HTTPException(422, "CSV parameterization is only available for HAR imports.")
    raw = await _read_upload(request)
    try:
        headers, row_count = await run_in_threadpool(inspect_csv, raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not request.app.state.db.update_parameter_csv(script_id, headers, row_count):
        raise HTTPException(404, f"Script '{script_id}' not found")
    audit.info("PARAMETER CSV script=%s by=%s columns=%d rows=%d", script_id, who, len(headers), row_count)
    return {"csv_headers": headers, "csv_row_count": row_count}


@router.put("/{script_id}/parameters")
def update_parameters(
    script_id: str,
    update: ParameterizationUpdate,
    request: Request,
    who: str = Depends(require_api_key),
):
    script = _get_or_404(request, script_id)
    if script["source"] != "har":
        raise HTTPException(422, "CSV parameterization is only available for HAR imports.")
    try:
        items, parameterization = apply_parameters(
            script["items"], script["parameterization"], [r.model_dump() for r in update.parameters],
            script["csv_headers"],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    parameterization["recycle"] = update.recycle
    parameterization["sharing_mode"] = update.sharing_mode
    if not request.app.state.db.update_parameterization(script_id, parameterization, items):
        raise HTTPException(404, f"Script '{script_id}' not found")
    audit.info("PARAMETERS script=%s by=%s rules=%d", script_id, who, len(update.parameters))
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
        included = {i for t in view["transactions"] for i in t["items"]}
        correlations = []
        for rule in script["correlations"]:
            used_targets = set(rule["target_indices"]) & included
            if not used_targets:
                continue
            if not rule["enabled"]:
                raise HTTPException(
                    422,
                    f"Correlation variable ${{{rule['variable']}}} is disabled but still used by a kept request. "
                    "Re-enable the rule or exclude its target requests.",
                )
            if rule["source_index"] not in included:
                raise HTTPException(
                    422,
                    f"Correlation variable ${{{rule['variable']}}} is used by a kept request, "
                    "but its source request is excluded. Keep the source request or exclude its targets.",
                )
            if rule["source_index"] in included:
                correlations.append(rule)
        xml = build_recorded_plan(
            script["name"], groups, variables=script["variables"], users=users,
            ramp_up_seconds=ramp_up_seconds, loops=loops, correlations=correlations,
            parameterization=script["parameterization"],
            csv_headers=script["csv_headers"],
            assertions=script["assertions"],
        )
    else:
        xml = rebuild(script["original"], view["transactions"])
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", script["name"]).strip("-.")[:60] or script_id
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.jmx"'})


@router.post("/{script_id}/validate")
def validate_script(script_id: str, request: Request, _: str = Depends(require_api_key)):
    script = _get_or_404(request, script_id)
    if script["source"] != "har":
        raise HTTPException(422, "HAR comparison validation is only available for HAR imports.")
    _, view = _evaluate(script)
    return validate_har(
        name=script["name"],
        items=script["items"],
        transactions=view["transactions"],
        correlations=script["correlations"],
        variables=script["variables"],
        parameterization=script["parameterization"],
        csv_headers=script["csv_headers"],
        assertions=script["assertions"],
    )


@router.delete("/{script_id}", status_code=204)
def delete_script(script_id: str, request: Request, who: str = Depends(require_api_key)):
    debug_runs = request.app.state.db.list_debug_runs(script_id)
    if any(run["status"] == "running" for run in debug_runs):
        raise HTTPException(409, "Stop or wait for active debug runs before deleting this import.")
    if not request.app.state.db.delete_script(script_id):
        raise HTTPException(404, f"Script '{script_id}' not found")
    for run in debug_runs:
        paths = debug_paths(request.app.state.settings, run["id"])
        if paths["dir"].exists():
            shutil.rmtree(paths["dir"])
    audit.info("DELETE script=%s by=%s", script_id, who)
    return Response(status_code=204)
