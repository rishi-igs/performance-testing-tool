"""Shared FastAPI dependencies: who is calling, what they may do, which projects they see.

Three ways to sign in, checked in this order:
  1. the API_KEY setting (X-API-Key header or api_key cookie): an administrator, as before;
  2. a personal API token (X-API-Key: pt_...), for the CLI and pipelines;
  3. a session cookie from POST /auth/login. Writes with a session also need the header
     X-Requested-With: console (the dashboard sends it), so other sites cannot forge them.
With no user accounts and no API_KEY the tool is open, as it always was: only run it that way
on a trusted machine. Once the first account exists, every call needs one of the above.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from .db import DEFAULT_PROJECT
from .services.auth import SESSION_COOKIE, TOKEN_PREFIX, Principal, hash_token
from .services.security import api_key_ok

audit = logging.getLogger("perf.audit")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def user_principal(request: Request, user: dict | None, via: str) -> Principal | None:
    if not user or user["disabled"]:
        return None
    projects = None if user["role"] == "admin" else request.app.state.db.user_projects(user["id"])
    return Principal(user["username"], user_id=user["id"], role=user["role"], projects=projects, mode=via)


def principal(request: Request) -> Principal:
    settings, db = request.app.state.settings, request.app.state.db
    client = request.client.host if request.client else "unknown"
    key = request.headers.get("x-api-key") or request.cookies.get("api_key")
    if key and settings.api_key and api_key_ok(key, settings):
        return Principal(f"key@{client}", role="admin", mode="api_key")
    if key and key.startswith(TOKEN_PREFIX):
        token = db.get_token_by_hash(hash_token(key))
        found = user_principal(request, db.get_user(token["user_id"]) if token else None, "token")
        if found:
            return found
        raise HTTPException(401, "Invalid API token")
    session = request.cookies.get(SESSION_COOKIE)
    if session:
        s = db.get_session(hash_token(session))
        found = user_principal(request, db.get_user(s["user_id"]) if s else None, "session")
        if found:
            if request.method not in SAFE_METHODS and request.headers.get("x-requested-with") != "console":
                raise HTTPException(403, "Missing the X-Requested-With: console header")
            return found
    if not settings.api_key and db.count_users() == 0:
        return Principal(f"anonymous@{client}", role="admin", mode="open")
    raise HTTPException(401, "Sign in, or send an API key")


def _require(request: Request, role: str) -> Principal:
    who = principal(request)
    if not who.can(role):
        raise HTTPException(403, f"This needs the {role} role")
    request.state.principal = who
    return who


def viewer(request: Request) -> Principal:
    """Read access."""
    return _require(request, "viewer")


def tester(request: Request) -> Principal:
    """Create, change and run scripts, scenarios and tests."""
    return _require(request, "tester")


def admin(request: Request) -> Principal:
    """Manage users, projects, load generators and monitors."""
    return _require(request, "admin")


# The old name: every endpoint used to need just the API key.
require_api_key = viewer


def require_console(request: Request) -> None:
    """Sign-in forms are only accepted from the dashboard (blocks cross-site login and setup)."""
    if request.headers.get("x-requested-with") != "console":
        raise HTTPException(403, "Missing the X-Requested-With: console header")


def visible_projects(request: Request) -> set[str] | None:
    who = getattr(request.state, "principal", None)
    return who.projects if who is not None else None


def project_filter(request: Request, project: str | None) -> set[str] | None:
    """Projects a list shows: the one asked for (if visible), or every visible one (None = all)."""
    projects = visible_projects(request)
    if project:
        return {project} if projects is None or project in projects else set()
    return projects


def ensure_project(request: Request, project_id: str | None) -> None:
    """Hide things in projects the caller is not a member of (404, so their existence does not leak)."""
    who = getattr(request.state, "principal", None)
    if who is not None and not who.sees(project_id or DEFAULT_PROJECT):
        raise HTTPException(404, "Not found")


def target_project(request: Request, project: str | None) -> str:
    """The project a new item goes into: the one asked for, or Default, or the caller's first project."""
    db = request.app.state.db
    seen = visible_projects(request)
    if project:
        if not db.get_project(project) or (seen is not None and project not in seen):
            raise HTTPException(404, f"Project '{project}' not found")
        return project
    if seen is None or DEFAULT_PROJECT in seen:
        return DEFAULT_PROJECT
    if not seen:
        raise HTTPException(403, "You are not a member of any project yet; ask an administrator")
    return sorted(seen)[0]
