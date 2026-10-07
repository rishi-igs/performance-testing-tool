"""Shared FastAPI dependencies: authentication and audit identity."""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from .services.security import api_key_ok

audit = logging.getLogger("perf.audit")


def require_api_key(request: Request) -> str:
    """Accept the key via X-API-Key header or the api_key cookie (used by the dashboard).

    Returns the caller identity for audit logs. With no API_KEY configured the
    tool is open: only run it that way on a trusted local machine.
    """
    settings = request.app.state.settings
    provided = request.headers.get("x-api-key") or request.cookies.get("api_key")
    if not api_key_ok(provided, settings):
        raise HTTPException(status_code=401, detail="Missing or invalid API key")
    client = request.client.host if request.client else "unknown"
    return f"{'key' if settings.api_key else 'anonymous'}@{client}"
