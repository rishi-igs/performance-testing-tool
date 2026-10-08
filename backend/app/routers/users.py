"""User accounts (administrators only). Roles: viewer (read), tester (create and run), admin (everything)."""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..db import Database
from ..deps import admin, audit
from ..models.account import UserIn, UserUpdate
from ..services import auth
from ..services.auth import Principal
from .auth import new_account_problem

router = APIRouter(prefix="/users", tags=["users"])


def _memberships(db: Database) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in db.list_projects():
        for user_id in p["members"]:
            out.setdefault(user_id, []).append(p["id"])
    return out


def _public(db: Database, user: dict[str, Any]) -> dict[str, Any]:
    return {k: user[k] for k in ("id", "username", "role", "created_at", "last_login_at")} | {
        "disabled": bool(user["disabled"]), "projects": sorted(_memberships(db).get(user["id"], []))}


def _check_projects(db: Database, projects: list[str]) -> None:
    known = {p["id"] for p in db.list_projects()}
    unknown = [p for p in projects if p not in known]
    if unknown:
        raise HTTPException(422, f"Unknown project(s): {', '.join(unknown)}")


def _get(db: Database, user_id: str) -> dict[str, Any]:
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(404, f"User '{user_id}' not found")
    return user


def _keep_an_admin(db: Database, user: dict[str, Any]) -> None:
    if user["role"] == "admin" and not user["disabled"] and db.count_admins(excluding=user["id"]) == 0:
        raise HTTPException(409, "This is the last administrator. Make someone else an administrator first.")


@router.get("")
def list_users(request: Request, _: Principal = Depends(admin)):
    db = request.app.state.db
    members = _memberships(db)
    return [u | {"disabled": bool(u["disabled"]), "projects": sorted(members.get(u["id"], []))} for u in db.list_users()]


@router.post("", status_code=201)
def create_user(body: UserIn, request: Request, who: Principal = Depends(admin)):
    db = request.app.state.db
    name = auth.normalize_username(body.username)
    problem = new_account_problem(name, body.password)
    if problem:
        raise HTTPException(422, problem)
    _check_projects(db, body.projects)
    user_id = f"u_{uuid.uuid4().hex[:10]}"
    try:
        db.create_user(user_id, name, auth.hash_password(body.password), body.role)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"A user named '{name}' already exists") from exc
    db.set_user_projects(user_id, body.projects)
    audit.info("USER create=%s role=%s projects=%s by=%s", name, body.role, ",".join(body.projects), who)
    return _public(db, db.get_user(user_id))


@router.put("/{user_id}")
def update_user(user_id: str, body: UserUpdate, request: Request, who: Principal = Depends(admin)):
    db = request.app.state.db
    user = _get(db, user_id)
    fields: dict[str, Any] = {}
    if body.role is not None and body.role != user["role"]:
        _keep_an_admin(db, user)
        fields["role"] = body.role
    if body.disabled is not None and body.disabled != bool(user["disabled"]):
        if body.disabled:
            if user_id == who.user_id:
                raise HTTPException(409, "You cannot disable your own account")
            _keep_an_admin(db, user)
        fields["disabled"] = int(body.disabled)
    if body.password is not None:
        problem = auth.password_problem(body.password, user["username"])
        if problem:
            raise HTTPException(422, f"Password: {problem}")
        fields["password_hash"] = auth.hash_password(body.password)
    if body.projects is not None:
        _check_projects(db, body.projects)
    db.update_user(user_id, **fields)
    if body.projects is not None:
        db.set_user_projects(user_id, body.projects)
    if fields.get("disabled") or "password_hash" in fields:
        db.delete_user_sessions(user_id)     # disabled or reset: sign them out everywhere
    changed = [k.replace("password_hash", "password") for k in fields] + (["projects"] if body.projects is not None else [])
    audit.info("USER update=%s changed=%s by=%s", user["username"], ",".join(changed) or "nothing", who)
    return _public(db, db.get_user(user_id))


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: str, request: Request, who: Principal = Depends(admin)):
    db = request.app.state.db
    user = _get(db, user_id)
    if user_id == who.user_id:
        raise HTTPException(409, "You cannot delete your own account")
    _keep_an_admin(db, user)
    db.delete_user(user_id)
    audit.info("USER delete=%s by=%s", user["username"], who)
    return Response(status_code=204)
