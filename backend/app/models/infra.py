"""Load generators (remote JMeter engines) and server monitors."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:.]+\]?$")


class GeneratorIn(BaseModel):
    """A machine running jmeter-server (LoadRunner: a load generator)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(1099, ge=1, le=65535, description="jmeter-server's RMI registry port")
    enabled: bool = True

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if not _HOST.match(v):
            raise ValueError("host must be a host name or an IP address")
        return v


class PromMetric(BaseModel):
    """One value read from a Prometheus endpoint: the sum of the series matching name and labels."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=80)
    unit: str = Field("", max_length=20)
    metric: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    labels: dict[str, str] = Field(default_factory=dict)
    type: Literal["gauge", "rate"] = "gauge"
    scale: float = 1.0


class MonitorIn(BaseModel):
    """Where to read server metrics during a run (LoadRunner: online monitors)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    kind: Literal["agent", "prometheus", "local"]
    url: str | None = Field(None, max_length=500)
    token: str | None = Field(None, max_length=500, description="agent only; never returned by the API")
    preset: Literal["none", "node_exporter", "windows_exporter"] = "none"
    metrics: list[PromMetric] = Field(default_factory=list, max_length=50)
    interval_seconds: int = Field(5, ge=2, le=60)
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "MonitorIn":
        if self.kind in ("agent", "prometheus"):
            if not self.url or not re.match(r"^https?://[^/\s]+", self.url):
                raise ValueError("this monitor needs an http(s) URL")
        if self.kind == "prometheus" and self.preset == "none" and not self.metrics:
            raise ValueError("choose a preset or list the metrics to read")
        return self
