"""Request model for creating a performance test and its traffic profile."""
from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-api-key", "x-auth-token"}
TEST_TYPES = ("load", "stress", "spike", "soak", "volume", "scalability", "breakpoint")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|m|min|h|hr|d)?\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86_400}


def seconds(value: Any) -> Any:
    """30, "30", "30s", "5m", "24h" -> whole seconds; anything else is left for validation to reject."""
    if isinstance(value, str):
        m = _DURATION.match(value)
        if not m:
            raise ValueError(f"{value!r} is not a duration (use seconds, or a number with s, m or h)")
        return round(float(m.group(1)) * _UNIT_SECONDS[(m.group(2) or "s").lower()])
    return value


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_error_rate_percent: float = Field(1.0, ge=0, le=100)
    max_p95_ms: int | None = Field(1000, ge=1)


class TrafficStep(BaseModel):
    """At `time_seconds`, change to `users` virtual users over `ramp_seconds` (0 = at once).

    Users above the new level stop (the last ones started stop first); 0 users is a pause.
    `time` and `ramp` are accepted as aliases written with units, e.g. {"time": "2m", "ramp": "30s"}.
    """

    model_config = ConfigDict(extra="forbid")

    time_seconds: int = Field(ge=0)
    users: int = Field(ge=0)
    ramp_seconds: int = Field(0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            for alias, name in (("time", "time_seconds"), ("ramp", "ramp_seconds")):
                if alias in data:
                    if name in data:
                        raise ValueError(f"give either {alias} or {name}, not both")
                    data[name] = data.pop(alias)
            for name in ("time_seconds", "ramp_seconds"):
                if name in data:
                    data[name] = seconds(data[name])
        return data


def check_profile(profile: list[TrafficStep], duration_seconds: int) -> None:
    """Steps start at 0, follow each other without overlapping ramps and end within the test."""
    if profile[0].time_seconds != 0:
        raise ValueError("profile must start at time_seconds 0")
    level = 0
    for n, (previous, current) in enumerate(zip([None, *profile], profile), 1):
        if previous is not None:
            if current.time_seconds <= previous.time_seconds:
                raise ValueError("profile time_seconds values must be strictly increasing")
            if current.time_seconds < previous.time_seconds + previous.ramp_seconds:
                raise ValueError(f"step {n} starts at {current.time_seconds}s, before the ramp of step {n - 1} "
                                 f"ends ({previous.time_seconds + previous.ramp_seconds}s)")
        if current.users == level:
            raise ValueError(f"step {n} does not change the number of users ({level})")
        level = current.users
    if max(step.users for step in profile) < 1:
        raise ValueError("at least one profile step needs users")
    last = profile[-1]
    if last.time_seconds >= duration_seconds:
        raise ValueError("the final profile step must start before duration_seconds")
    if last.time_seconds + last.ramp_seconds > duration_seconds:
        raise ValueError(f"the final step's ramp ends at {last.time_seconds + last.ramp_seconds}s, "
                         f"after the test ends ({duration_seconds}s)")


class StopCondition(BaseModel):
    """Stop the test early once the last `for_seconds` stay above a limit (a breaking point)."""

    model_config = ConfigDict(extra="forbid")

    error_rate_percent: float | None = Field(None, gt=0, le=100)
    p95_ms: int | None = Field(None, ge=1)
    for_seconds: int = Field(10, ge=1, le=3600, description="how long the limit must be exceeded")

    @model_validator(mode="after")
    def _check(self) -> "StopCondition":
        if self.error_rate_percent is None and self.p95_ms is None:
            raise ValueError("stop_when needs error_rate_percent, p95_ms or both")
        return self


class LoadTestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    target_url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = Field(default=None, max_length=1_000_000)
    test_type: Literal[TEST_TYPES] = "load"
    users: int = Field(10, ge=1)
    duration_seconds: int = Field(60, ge=1)
    ramp_up_seconds: int = Field(0, ge=0)
    profile: list[TrafficStep] | None = Field(default=None, min_length=2, max_length=100)
    think_time_ms: int = Field(0, ge=0, le=60_000)
    timeout_ms: int = Field(30_000, ge=100, le=300_000)
    expected_status_codes: list[int] = Field(default_factory=lambda: [200], min_length=1)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    stop_when: StopCondition | None = None

    @model_validator(mode="before")
    @classmethod
    def _durations(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            for name in ("duration_seconds", "ramp_up_seconds"):
                if name in data:
                    data[name] = seconds(data[name])
        return data

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
    def _validate_load_schedule(self) -> "LoadTestConfig":
        if self.ramp_up_seconds > self.duration_seconds:
            raise ValueError("ramp_up_seconds cannot be longer than duration_seconds")
        if self.profile is not None:
            if self.ramp_up_seconds:
                raise ValueError("ramp_up_seconds must be 0 when a profile is set (give the step a ramp instead)")
            check_profile(self.profile, self.duration_seconds)
            peak = max(step.users for step in self.profile)
            if "users" not in self.model_fields_set:
                self.users = peak
            elif self.users != peak:
                raise ValueError(f"users must equal the profile's peak ({peak} users)")
        return self

    def masked(self) -> dict:
        """Config safe to return from the API (secrets in headers hidden)."""
        data = self.model_dump()
        data["headers"] = mask_headers(self.headers)
        return data


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("********" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}
