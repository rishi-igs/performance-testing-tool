"""Projects: separate workspaces for scripts, scenarios, runs and schedules.

Administrators see every project; other users see the projects they are members of.
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..db import DEFAULT_PROJECT, Database
from ..deps import admin, audit, viewer
from ..models.account import ProjectIn
from ..services.auth import Principal

router = APIRouter(prefix="/projects", tags=["projects"])


def _view(db: Database, project: dict[str, Any], who: Principal) -> dict[str, Any]:
    out = {k: project[k] for k in ("id", "name", "created_at")}
    if who.can("admin"):
        names = {u["id"]: u["username"] for u in db.list_users()}
        out["members"] = [{"id": m, "username": names.get(m, m)} for m in project["members"]]
        out["usage"] = db.project_usage(project["id"])
    return out


def _check_members(db: Database, members: list[str]) -> None:
    known = {u["id"] for u in db.list_users()}
    unknown = [m for m in members if m not in known]
    if unknown:
        raise HTTPException(422, f"Unknown user(s): {', '.join(unknown)}")


@router.get("")
def list_projects(request: Request, who: Principal = Depends(viewer)):
    db = request.app.state.db
    return [_view(db, p, who) for p in db.list_projects() if who.sees(p["id"])]


@router.post("", status_code=201)
def create_project(body: ProjectIn, request: Request, who: Principal = Depends(admin)):
    db = request.app.state.db
    _check_members(db, body.members)
    project_id = f"p_{uuid.uuid4().hex[:8]}"
    try:
        db.create_project(project_id, body.name)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"A project named '{body.name}' already exists") from exc
    db.update_project(project_id, body.name, body.members)
    audit.info("PROJECT create=%s name=%s by=%s", project_id, body.name, who)
    return _view(db, db.get_project(project_id), who)


@router.put("/{project_id}")
def update_project(project_id: str, body: ProjectIn, request: Request, who: Principal = Depends(admin)):
    db = request.app.state.db
    if not db.get_project(project_id):
        raise HTTPException(404, f"Project '{project_id}' not found")
    _check_members(db, body.members)
    try:
        db.update_project(project_id, body.name, body.members)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"A project named '{body.name}' already exists") from exc
    audit.info("PROJECT update=%s name=%s members=%d by=%s", project_id, body.name, len(body.members), who)
    return _view(db, db.get_project(project_id), who)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, request: Request, who: Principal = Depends(admin)):
    db = request.app.state.db
    if project_id == DEFAULT_PROJECT:
        raise HTTPException(409, "The Default project cannot be deleted")
    if not db.get_project(project_id):
        raise HTTPException(404, f"Project '{project_id}' not found")
    used = db.project_usage(project_id)
    if used:
        raise HTTPException(409, f"The project still holds {used} script(s), scenario(s), run(s) or schedule(s). "
                                 "Delete them first.")
    db.delete_project(project_id)
    audit.info("PROJECT delete=%s by=%s", project_id, who)
    return Response(status_code=204)
