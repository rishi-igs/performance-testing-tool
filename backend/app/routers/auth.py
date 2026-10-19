"""Sign-in, the first administrator account, your own password and API tokens."""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..db import now_iso
from ..deps import admin, audit, principal, require_console, user_principal, viewer
from ..models.account import Credentials, PasswordChange, TokenIn
from ..services import auth
from ..services.auth import Principal

router = APIRouter(prefix="/auth", tags=["auth"])


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def new_account_problem(username: str, password: str) -> str | None:
    problem = auth.username_problem(username)
    if problem:
        return f"User name: {problem}"
    problem = auth.password_problem(password, username)
    return f"Password: {problem}" if problem else None


def start_session(request: Request, response: Response, user: dict[str, Any]) -> None:
    token = auth.new_secret()
    expires = datetime.now(timezone.utc) + timedelta(hours=auth.SESSION_HOURS)
    request.app.state.db.create_session(auth.hash_token(token), user["id"], expires.isoformat(timespec="seconds"))
    secure = request.app.state.settings.secure_cookies or request.url.scheme == "https"
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_HOURS * 3600, path="/",
                        httponly=True, samesite="strict", secure=secure)


def _me(request: Request, who: Principal | None) -> dict[str, Any]:
    db, settings = request.app.state.db, request.app.state.settings
    projects = [{"id": p["id"], "name": p["name"]} for p in db.list_projects() if who is not None and who.sees(p["id"])]
    return {"authenticated": who is not None, "mode": who.mode if who else None, "name": str(who) if who else None,
            "role": who.role if who else None, "user_id": who.user_id if who else None, "projects": projects,
            "setup_needed": db.count_users() == 0, "api_key_enabled": bool(settings.api_key)}


@router.get("/me")
def me(request: Request):
    """Who is calling, and whether the dashboard should show sign-in or first-account setup."""
    try:
        who = principal(request)
    except HTTPException:
        who = None
    return _me(request, who)


@router.post("/setup", status_code=201)
def setup(body: Credentials, request: Request, response: Response, who: Principal = Depends(admin)):
    """Create the first administrator. Only while no account exists; after that, sign-in is required."""
    if who.mode == "open":
        require_console(request)
    db = request.app.state.db
    name = auth.normalize_username(body.username)
    problem = new_account_problem(name, body.password)
    if problem:
        raise HTTPException(422, problem)
    user_id = f"u_{uuid.uuid4().hex[:10]}"
    if not db.create_first_user(user_id, name, auth.hash_password(body.password)):
        raise HTTPException(409, "An account already exists: sign in instead")
    audit.info("SETUP first-admin=%s by=%s", name, who)
    user = db.get_user(user_id)
    start_session(request, response, user)
    return _me(request, user_principal(request, user, "session"))


@router.post("/login")
def login(body: Credentials, request: Request, response: Response):
    require_console(request)
    db, throttle = request.app.state.db, request.app.state.login_throttle
    name, client = auth.normalize_username(body.username), _client(request)
    keys = (f"user:{name}", f"ip:{client}")
    wait = throttle.blocked_for(*keys)
    if wait:
        audit.warning("LOGIN blocked user=%s from=%s", name, client)
        raise HTTPException(429, f"Too many failed sign-ins. Try again in {math.ceil(wait / 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    user = db.get_user_by_name(name)
    if not (auth.verify_password(body.password, user["password_hash"]) if user else auth.dummy_verify(body.password)):
        throttle.failed(*keys)
        audit.warning("LOGIN failed user=%s from=%s", name, client)
        raise HTTPException(401, "Wrong user name or password")
    if user["disabled"]:
        audit.warning("LOGIN disabled-account user=%s from=%s", name, client)
        raise HTTPException(403, "This account is disabled. Ask an administrator.")
    throttle.succeeded(keys[0])
    db.update_user(user["id"], last_login_at=now_iso())
    start_session(request, response, user)
    audit.info("LOGIN user=%s from=%s", name, client)
    return _me(request, user_principal(request, db.get_user(user["id"]), "session"))


@router.post("/logout", status_code=204)
def logout(request: Request):
    require_console(request)
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        request.app.state.db.delete_session(auth.hash_token(token))
    response = Response(status_code=204)
    response.delete_cookie(auth.SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


@router.post("/password", status_code=204)
def change_password(body: PasswordChange, request: Request, who: Principal = Depends(viewer)):
    """Change your own password; every other session of yours is signed out."""
    if not who.user_id:
        raise HTTPException(400, "Only user accounts have a password")
    db, throttle = request.app.state.db, request.app.state.login_throttle
    key = f"password:{who.user_id}"
    if throttle.blocked_for(key):
        raise HTTPException(429, "Too many wrong passwords. Try again later.")
    user = db.get_user(who.user_id)
    if not auth.verify_password(body.current_password, user["password_hash"]):
        throttle.failed(key)
        audit.warning("PASSWORD wrong-current user=%s", who)
        raise HTTPException(403, "The current password is wrong")
    problem = auth.password_problem(body.new_password, user["username"])
    if problem:
        raise HTTPException(422, f"Password: {problem}")
    throttle.succeeded(key)
    db.update_user(user["id"], password_hash=auth.hash_password(body.new_password))
    db.delete_user_sessions(user["id"])
    response = Response(status_code=204)
    if who.mode == "session":
        start_session(request, response, user)
    audit.info("PASSWORD changed user=%s", who)
    return response


# ---- personal API tokens (for the CLI and pipelines: X-API-Key: pt_...) -------------------------

@router.get("/tokens")
def list_tokens(request: Request, who: Principal = Depends(viewer)):
    return request.app.state.db.list_tokens(who.user_id) if who.user_id else []


@router.post("/tokens", status_code=201)
def create_token(body: TokenIn, request: Request, who: Principal = Depends(viewer)):
    """A token acts as you, with your role and projects. It is shown once; only its hash is stored."""
    if who.mode != "session":
        raise HTTPException(400, "Create API tokens in the dashboard while signed in to your account")
    token = auth.new_secret(auth.TOKEN_PREFIX)
    token_id = f"k_{uuid.uuid4().hex[:8]}"
    request.app.state.db.create_token(token_id, who.user_id, body.name, auth.hash_token(token))
    audit.info("TOKEN create=%s user=%s", token_id, who)
    return {"id": token_id, "name": body.name, "token": token}


@router.delete("/tokens/{token_id}", status_code=204)
def delete_token(token_id: str, request: Request, who: Principal = Depends(viewer)):
    if not who.user_id or not request.app.state.db.delete_token(token_id, who.user_id):
        raise HTTPException(404, f"Token '{token_id}' not found")
    audit.info("TOKEN delete=%s user=%s", token_id, who)
    return Response(status_code=204)
