"""Load generators (remote jmeter-server engines) and server monitors."""
from __future__ import annotations

import socket
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from ..deps import admin, audit, tester
from ..deps import viewer as require_api_key
from ..models.infra import GeneratorIn, MonitorIn
from ..services.monitoring import check_monitor
from ..services.security import TargetNotAllowed, check_target

router = APIRouter(tags=["infrastructure"])


def check_generator(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    """Can the console open a connection to jmeter-server's RMI registry?"""
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        return {"ok": False, "error": f"cannot connect to {host}:{port}: {exc}", "checked_at": time.time()}
    return {"ok": True, "error": None, "connect_ms": round((time.monotonic() - started) * 1000), "checked_at": time.time()}


def _public_monitor(m: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(m["config"])
    cfg["token"] = "********" if cfg.get("token") else None
    return {**m, "config": cfg}


def _get(request: Request, table: str, item_id: str) -> dict[str, Any]:
    item = request.app.state.db.get_resource(table, item_id)
    if not item:
        raise HTTPException(404, f"{table[:-1].capitalize()} '{item_id}' not found")
    return item


def _check_monitor_url(request: Request, cfg: MonitorIn) -> None:
    """The console fetches monitor URLs itself, so they pass the same target rules as tests."""
    if cfg.url:
        try:
            check_target(cfg.url, request.app.state.settings)
        except TargetNotAllowed as exc:
            raise HTTPException(422, f"Monitor URL: {exc}") from exc


# ---- load generators --------------------------------------------------------------------

@router.get("/generators")
def list_generators(request: Request, _: str = Depends(require_api_key)):
    return request.app.state.db.list_resources("generators")


@router.post("/generators", status_code=201)
def create_generator(body: GeneratorIn, request: Request, who: str = Depends(admin)):
    gid = f"g_{uuid.uuid4().hex[:8]}"
    request.app.state.db.insert_resource("generators", gid, body.model_dump())
    audit.info("GENERATOR create=%s host=%s by=%s", gid, body.host, who)
    return _get(request, "generators", gid)


@router.put("/generators/{gid}")
def update_generator(gid: str, body: GeneratorIn, request: Request, who: str = Depends(admin)):
    _get(request, "generators", gid)
    request.app.state.db.update_resource("generators", gid, config=body.model_dump())
    audit.info("GENERATOR update=%s host=%s by=%s", gid, body.host, who)
    return _get(request, "generators", gid)


@router.delete("/generators/{gid}", status_code=204)
def delete_generator(gid: str, request: Request, who: str = Depends(admin)):
    if not request.app.state.db.delete_resource("generators", gid):
        raise HTTPException(404, f"Generator '{gid}' not found")
    audit.info("GENERATOR delete=%s by=%s", gid, who)
    return Response(status_code=204)


@router.post("/generators/{gid}/check")
async def check_one_generator(gid: str, request: Request, who: str = Depends(tester)):
    g = _get(request, "generators", gid)
    status = await run_in_threadpool(check_generator, g["config"]["host"], g["config"]["port"])
    request.app.state.db.update_resource("generators", gid, status=status)
    return {**g, "status": status}


# ---- monitors -------------------------------------------------------------------------------

AGENT_FILE = Path(__file__).resolve().parents[2] / "agent" / "perf_agent.py"


@router.get("/agent/perf_agent.py")
def download_agent(_: str = Depends(require_api_key)):
    """The monitor agent to copy to a server (standard library only)."""
    return FileResponse(AGENT_FILE, media_type="text/x-python", filename="perf_agent.py")

@router.get("/monitors")
def list_monitors(request: Request, _: str = Depends(require_api_key)):
    return [_public_monitor(m) for m in request.app.state.db.list_resources("monitors")]


@router.post("/monitors", status_code=201)
def create_monitor(body: MonitorIn, request: Request, who: str = Depends(admin)):
    _check_monitor_url(request, body)
    mid = f"m_{uuid.uuid4().hex[:8]}"
    request.app.state.db.insert_resource("monitors", mid, body.model_dump())
    audit.info("MONITOR create=%s kind=%s by=%s", mid, body.kind, who)
    return _public_monitor(_get(request, "monitors", mid))


@router.put("/monitors/{mid}")
def update_monitor(mid: str, body: MonitorIn, request: Request, who: str = Depends(admin)):
    existing = _get(request, "monitors", mid)
    _check_monitor_url(request, body)
    cfg = body.model_dump()
    if cfg.get("token") == "********":          # the masked value came back unchanged
        cfg["token"] = existing["config"].get("token")
    request.app.state.db.update_resource("monitors", mid, config=cfg)
    audit.info("MONITOR update=%s kind=%s by=%s", mid, body.kind, who)
    return _public_monitor(_get(request, "monitors", mid))


@router.delete("/monitors/{mid}", status_code=204)
def delete_monitor(mid: str, request: Request, who: str = Depends(admin)):
    if not request.app.state.db.delete_resource("monitors", mid):
        raise HTTPException(404, f"Monitor '{mid}' not found")
    audit.info("MONITOR delete=%s by=%s", mid, who)
    return Response(status_code=204)


@router.post("/monitors/{mid}/check")
async def check_one_monitor(mid: str, request: Request, who: str = Depends(tester)):
    """Read the monitor now and show what it returns."""
    m = _get(request, "monitors", mid)
    _check_monitor_url(request, MonitorIn(**m["config"]))
    status = await run_in_threadpool(check_monitor, m["config"])
    request.app.state.db.update_resource("monitors", mid, status=status)
    return {**_public_monitor(m), "status": status}
