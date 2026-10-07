"""Request model for creating a performance test (Phase 1 scope)."""
from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-api-key", "x-auth-token"}


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_error_rate_percent: float = Field(1.0, ge=0, le=100)
    max_p95_ms: int | None = Field(1000, ge=1)


class LoadTestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    target_url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(default=None, max_length=1_000_000)
    users: int = Field(10, ge=1)
    duration_seconds: int = Field(60, ge=1)
    ramp_up_seconds: int = Field(0, ge=0)
    think_time_ms: int = Field(0, ge=0, le=60_000)
    timeout_ms: int = Field(30_000, ge=100, le=300_000)
    expected_status_codes: list[int] = Field(default_factory=lambda: [200], min_length=1)
    thresholds: Thresholds = Field(default_factory=Thresholds)

    @field_validator("target_url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        parts = urlsplit(v.strip())
        if parts.scheme not in {"http", "https"}:
            raise ValueError("target_url must start with http:// or https://")
        if not parts.hostname:
            raise ValueError("target_url must include a host name")
        if parts.username or parts.password:
            raise ValueError("Do not put credentials in the URL; use headers or a secret manager")
        return v.strip()

    @field_validator("headers")
    @classmethod
    def _valid_headers(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if not k or any(c in k for c in "\r\n:") or any(c in val for c in "\r\n"):
                raise ValueError(f"invalid header: {k!r}")
        return v

    @field_validator("expected_status_codes")
    @classmethod
    def _valid_codes(cls, v: list[int]) -> list[int]:
        if any(c < 100 or c > 599 for c in v):
            raise ValueError("status codes must be between 100 and 599")
        return v

    @model_validator(mode="after")
    def _ramp_within_duration(self) -> "LoadTestConfig":
        if self.ramp_up_seconds > self.duration_seconds:
            raise ValueError("ramp_up_seconds cannot be longer than duration_seconds")
        return self

    def masked(self) -> dict:
        """Config safe to return from the API (secrets in headers hidden)."""
        data = self.model_dump()
        data["headers"] = mask_headers(self.headers)
        return data


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("********" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}
