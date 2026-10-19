"""Response models returned by the API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

Status = Literal["running", "completed", "failed", "stopped"]


class TestCreated(BaseModel):
    __test__ = False
    id: str
    status: Status


class TestRecord(BaseModel):
    __test__ = False
    id: str
    name: str
    kind: str = "quick"
    scenario_id: str | None = None
    project_id: str = "p_default"
    status: Status
    config: dict[str, Any]
    summary: dict[str, Any] | None = None
    created_by: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
