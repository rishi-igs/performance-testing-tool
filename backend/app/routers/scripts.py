"""Import HAR or JMX recordings, filter and group their requests, design and export the script."""
from __future__ import annotations

import csv
import io
import logging
import re
import shutil
import uuid
import zipfile
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ..deps import audit, ensure_project, project_filter, target_project, tester
from ..deps import viewer as require_api_key
from ..models.script import CorrelationRule, DataFile, DesignUpdate, FilterRules, ScriptDesign, ScriptUpdate
from ..services import autocorrelate, request_filter, script_builder
from ..services.har_parser import ImportFailed, parse_har
from ..services.jmx_importer import parse_jmx
from ..services.test_executor import run_paths

router = APIRouter(prefix="/scripts", tags=["scripts"])
log = logging.getLogger("perf.scripts")
MAX_DATA_FILE_BYTES = 10 * 1024 * 1024


def get_script_or_404(request: Request, script_id: str) -> dict[str, Any]:
    script = request.app.state.db.get_script(script_id)
    if not script:
        raise HTTPException(404, f"Script '{script_id}' not found")
    ensure_project(request, script.get("project_id"))
    return script


def evaluate(script: dict[str, Any]) -> tuple[FilterRules, dict[str, Any]]:
    rules = FilterRules(**script["rules"])
    return rules, request_filter.evaluate(script["source"], script["items"], rules, script["overrides"])


def _detail(request: Request, script: dict[str, Any]) -> dict[str, Any]:
    rules, view = evaluate(script)
    design = ScriptDesign(**script["design"])
    files = request.app.state.db.script_files(script["id"])
    validation: dict[str, Any] = {"unresolved": {}, "used": {}, "empty": [], "scripted": False, "warnings": []}
    if view["transactions"]:
        try:
            built = script_builder.build(script, view, data=files)
            validation = {"unresolved": built.unresolved, "used": built.used, "empty": built.empty,
                          "scripted": built.scripted, "warnings": built.warnings}
        except Exception as exc:  # noqa: BLE001 - the review screen must still open
            log.exception("building script %s failed", script["id"])
            validation["warnings"] = [f"The plan could not be built: {exc}"]
    checked = {c.item for c in design.checks if c.item is not None}
    has_global_check = any(c.item is None for c in design.checks)
    status_only = [] if has_global_check else [
        v["index"] for v in view["items"] if v["include"] and v["kind"] == "http" and v["index"] not in checked]
    return {
        "id": script["id"], "name": script["name"], "source": script["source"], "project_id": script.get("project_id"),
        "created_by": script["created_by"], "created_at": script["created_at"], "updated_at": script["updated_at"],
        "rules": rules.model_dump(), "warnings": script["warnings"], "variables": script["variables"],
        "summary": view["summary"],
        "transactions": [
            {"number": t["number"], "name": t["name"], "thread_group": t["thread_group"], "requests": len(t["items"])}
            for t in view["transactions"]
        ],
        "items": [_with_replacements(v, design) for v in view["items"]],
        "design": design.model_dump(),
        "suggestions": script.get("suggestions") or {},
        "validation": {**validation, "status_only_checks": status_only},
        "files": [{"name": n, "bytes": len(c)} for n, c in sorted(files.items())],
    }


def _with_replacements(view_item: dict[str, Any], design: ScriptDesign) -> dict[str, Any]:
    """Show the URL as it will be sent: recorded text replaced by ${variables}."""
    url = view_item.get("url")
    if url:
        for r in design.replacements:
            url, _ = script_builder.replace_value(url, r.find, r.variable)
        for c in design.correlations:
            if c.replace and view_item["index"] > c.source:
                url, _ = script_builder.replace_value(url, c.replace, c.variable)
    return {**view_item, "url": url}


async def read_upload(request: Request, limit: int | None = None) -> bytes:
    limit = limit or request.app.state.settings.max_upload_bytes
    too_big = HTTPException(413, f"The file is larger than the {limit // (1024 * 1024)} MB limit.")
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


def _valid_correlations(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for rule in rules:
        try:
            out.append(CorrelationRule(**rule).model_dump())
        except ValidationError:
            log.warning("dropping an automatic correlation that failed validation: %s", rule.get("variable"))
    return out


async def _import(request: Request, who: str, source: str, name: str | None,
                  parser: Callable[[bytes], dict[str, Any]], project: str | None) -> dict[str, Any]:
    project_id = target_project(request, project)
    raw = await read_upload(request)
    try:
        parsed = await run_in_threadpool(parser, raw)
    except ImportFailed as exc:
        raise HTTPException(422, str(exc)) from exc
    script_id = f"s_{uuid.uuid4().hex[:10]}"
    title = (name or "").strip()[:100] or str(parsed["name"]).strip()[:100] or "Imported script"
    design = ScriptDesign(correlations=_valid_correlations(parsed.get("design", {}).get("correlations", [])))
    db = request.app.state.db
    db.insert_script(
        script_id, name=title, source=source, items=parsed["items"], variables=parsed["variables"],
        warnings=parsed["warnings"], rules=FilterRules().model_dump(),
        # A JMX export rebuilds the original file, so keep it. A HAR is not kept: only the sanitized requests are.
        original=raw if source == "jmx" else None, created_by=who,
        design=design.model_dump(), suggestions=parsed.get("suggestions") or {}, project_id=project_id,
    )
    try:
        await run_in_threadpool(parameterize_at_import, db, script_id)
    except Exception:  # noqa: BLE001 - the import itself worked; the user can still parameterize by hand
        log.exception("automatic parameterization of %s failed", script_id)
    audit.info("IMPORT script=%s by=%s source=%s requests=%d", script_id, who, source, len(parsed["items"]))
    return _detail(request, db.get_script(script_id))


def parameterize_at_import(db: Any, script_id: str) -> None:
    """What needs no replay: a recorded login becomes users.csv, single-use browser UUIDs and
    timestamps become generated values, and search terms are suggested as test data."""
    script = db.get_script(script_id)
    _, view = evaluate(script)
    if not view["transactions"]:
        return
    tree, _ = script_builder.build_tree(script, view)
    requests = autocorrelate.script_requests(tree, [i for t in view["transactions"] for i in t["items"]])
    taken = autocorrelate.design_names(script["design"], script.get("variables") or [])
    taken |= {v.lower() for v in script_builder.plan_variables(tree)}
    suggestions = dict(script.get("suggestions") or {})
    values = [v | {"uses": sum(1 for r in requests if script_builder.replace_value(autocorrelate.request_text(r), v["value"], "x")[1])}
              if v.get("value") else v for v in suggestions.get("parameters") or []]
    parameters, replacements, left = autocorrelate.generated_parameters(values, taken)
    data_files, files, notes = [], {}, []
    login = autocorrelate.find_login(requests)
    if login:
        made = autocorrelate.login_data_file(login, taken)
        where = f"#{login['index']} {script['items'][login['index']]['label']}"
        if made:
            data_file, content, login_replacements = made
            data_files.append(data_file)
            files[data_file["name"]] = content
            replacements += login_replacements
        else:
            notes.append(f"The login in {where} was not parameterized automatically: its user name or password also "
                         "appears elsewhere in the script, so replacing it everywhere could change other requests.")
    left += autocorrelate.search_suggestions(requests, taken)
    added: dict[str, list[str]] = {}

    def mutate(design: dict[str, Any]) -> dict[str, Any]:
        merged, result = autocorrelate.merge(design, parameters=parameters, replacements=replacements, data_files=data_files)
        added.update(result)
        return merged

    if parameters or data_files:
        db.update_design(script_id, mutate)
    for name in added.get("data_files", []):
        db.put_script_file(script_id, name, files[name])
        notes.append(f"The recorded login in {where} now reads ${{username}} and ${{password}} from {name}, which "
                     "holds the recorded account. Add a row for each test account (Parameters > Data files).")
    if added.get("parameters"):
        names = ", ".join(f"${{{p}}}" for p in added["parameters"])
        notes.append(f"Values the browser generated get a new value on each use: {names}.")
    db.update_suggestions(script_id, lambda s: {**s, "parameters": left, "auto": {"notes": notes}})


@router.post("/import-har", status_code=201)
async def import_har(request: Request, name: str | None = Query(None, max_length=100), project: str | None = None,
                     who: str = Depends(tester)):
    """Upload a browser HAR file as the raw request body."""
    return await _import(request, who, "har", name, parse_har, project)


@router.post("/import-jmx", status_code=201)
async def import_jmx(request: Request, name: str | None = Query(None, max_length=100), project: str | None = None,
                     who: str = Depends(tester)):
    """Upload a JMeter .jmx file as the raw request body."""
    return await _import(request, who, "jmx", name, parse_jmx, project)


@router.get("")
def list_scripts(request: Request, project: str | None = None, _: str = Depends(require_api_key)):
    return request.app.state.db.list_scripts(projects=project_filter(request, project))


@router.get("/{script_id}")
def get_script(script_id: str, request: Request, _: str = Depends(require_api_key)):
    return _detail(request, get_script_or_404(request, script_id))


@router.put("/{script_id}/filter")
def update_filter(script_id: str, update: ScriptUpdate, request: Request, who: str = Depends(tester)):
    """Save include/exclude choices and business-function names, and optionally replace the rules."""
    script = get_script_or_404(request, script_id)
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
    return _detail(request, get_script_or_404(request, script_id))


@router.put("/{script_id}/design")
def update_design(script_id: str, update: DesignUpdate, request: Request, who: str = Depends(tester)):
    """Replace correlations, parameters, data files, replacements, checks or run-time settings."""
    script = get_script_or_404(request, script_id)
    count = script["request_count"]
    for rule in update.correlations or []:
        if rule.source >= count:
            raise HTTPException(422, f"Correlation ${{{rule.variable}}}: request {rule.source} does not exist")
        if rule.origin == "manual":
            rule.used_in, rule.preview = [], None
    for check in update.checks or []:
        if check.item is not None and check.item >= count:
            raise HTTPException(422, f"Check: request {check.item} does not exist")
    if update.data_files is not None:
        existing = {f["name"]: f for f in script["design"].get("data_files", [])}
        for f in update.data_files:
            if f.name not in existing:
                raise HTTPException(422, f"Data file {f.name} was not uploaded")
            f.rows = existing[f.name].get("rows", 0)
    changes = update.model_dump(exclude_unset=True)

    def mutate(current: dict[str, Any]) -> dict[str, Any]:
        try:
            return ScriptDesign(**{**current, **changes}).model_dump()
        except ValidationError as exc:
            raise HTTPException(422, _validation_message(exc)) from exc

    if not request.app.state.db.update_design(script_id, mutate):
        raise HTTPException(404, f"Script '{script_id}' not found")
    audit.info("DESIGN script=%s by=%s parts=%s", script_id, who, ",".join(sorted(changes)))
    return _detail(request, get_script_or_404(request, script_id))


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}".lstrip(": ") for e in exc.errors())


@router.post("/{script_id}/files")
async def upload_data_file(script_id: str, request: Request,
                           name: str = Query(..., pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\.(csv|txt)$"),
                           header: bool = Query(True, description="first line holds the column names"),
                           who: str = Depends(tester)):
    """Upload a CSV of test data as the raw request body; each column becomes a ${variable}."""
    get_script_or_404(request, script_id)
    raw = await read_upload(request, MAX_DATA_FILE_BYTES)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "The data file must be UTF-8 text") from exc
    rows = [r for r in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in r)]
    if not rows:
        raise HTTPException(422, "The data file has no rows")
    stem = re.sub(r"[^A-Za-z0-9_]", "_", name.rsplit(".", 1)[0])
    if header:
        columns = [re.sub(r"[^A-Za-z0-9_]", "_", c.strip())[:60] or f"{stem}_{i}" for i, c in enumerate(rows[0], 1)]
        columns = [c if not c[0].isdigit() else f"v_{c}" for c in columns]
        data_rows = len(rows) - 1
    else:
        columns = [f"{stem}_{i}" for i in range(1, len(rows[0]) + 1)]
        data_rows = len(rows)
    if len(set(c.lower() for c in columns)) != len(columns):
        raise HTTPException(422, "Column names must be unique: " + ", ".join(columns))

    def mutate(design: dict[str, Any]) -> dict[str, Any]:
        files = [f for f in design.get("data_files", []) if f["name"] != name]
        previous = next((f for f in design.get("data_files", []) if f["name"] == name), {})
        files.append(DataFile(**{**previous, "name": name, "columns": columns, "rows": data_rows,
                                 "first_line_header": header}).model_dump())
        try:
            return ScriptDesign(**{**design, "data_files": files}).model_dump()
        except ValidationError as exc:
            raise HTTPException(422, _validation_message(exc)) from exc

    request.app.state.db.update_design(script_id, mutate)
    request.app.state.db.put_script_file(script_id, name, raw)
    audit.info("DATAFILE script=%s by=%s file=%s rows=%d", script_id, who, name, data_rows)
    return _detail(request, get_script_or_404(request, script_id))


@router.delete("/{script_id}/files/{name}")
def delete_data_file(script_id: str, name: str, request: Request, who: str = Depends(tester)):
    get_script_or_404(request, script_id)
    request.app.state.db.delete_script_file(script_id, name)
    request.app.state.db.update_design(script_id, lambda d: {
        **d, "data_files": [f for f in d.get("data_files", []) if f["name"] != name]})
    return _detail(request, get_script_or_404(request, script_id))


@router.get("/{script_id}/export-jmx")
def export_jmx(
    script_id: str,
    request: Request,
    users: int = Query(1, ge=1, description="HAR imports only; a JMX import keeps its own thread groups"),
    ramp_up_seconds: int = Query(0, ge=0, le=3600),
    loops: int = Query(1, ge=1, le=100_000),
    _: str = Depends(require_api_key),
):
    """The plan as .jmx, or a .zip with the plan and its data files when the script uses any."""
    settings = request.app.state.settings
    if users > settings.max_users:
        raise HTTPException(422, f"users cannot exceed {settings.max_users}")
    script = get_script_or_404(request, script_id)
    _, view = evaluate(script)
    if not view["transactions"]:
        raise HTTPException(422, "Nothing to export: every request is excluded.")
    built = script_builder.build(script, view, users=users, ramp_up_seconds=ramp_up_seconds, loops=loops,
                                 data=request.app.state.db.script_files(script_id))
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", script["name"]).strip("-.")[:60] or script_id
    if not built.files:
        return Response(built.xml, media_type="application/xml",
                        headers={"Content-Disposition": f'attachment; filename="{filename}.jmx"'})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{filename}.jmx", built.xml)
        for path, content in built.files.items():
            z.writestr(path, content)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.zip"'})


@router.delete("/{script_id}", status_code=204)
def delete_script(script_id: str, request: Request, who: str = Depends(tester)):
    runs = request.app.state.db.delete_script(script_id)
    if runs is None:
        raise HTTPException(404, f"Script '{script_id}' not found")
    for run_id in runs:
        shutil.rmtree(run_paths(request.app.state.settings, run_id)["dir"], ignore_errors=True)
    audit.info("DELETE script=%s by=%s", script_id, who)
    return Response(status_code=204)
