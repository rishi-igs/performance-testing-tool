"""Runtime settings, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    jmeter_bin: str
    data_dir: Path
    api_key: str | None
    allow_private_targets: bool
    allowed_hosts: tuple[str, ...]
    max_users: int
    max_duration_seconds: int
    max_concurrent_tests: int
    max_upload_bytes: int = 50 * 1024 * 1024

    @property
    def db_path(self) -> Path:
        return self.data_dir / "perf_tool.sqlite3"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("PERF_DATA_DIR", "data")).resolve()
    hosts = tuple(
        h.strip().lower()
        for h in os.environ.get("ALLOWED_HOSTS", "").split(",")
        if h.strip()
    )
    return Settings(
        jmeter_bin=os.environ.get("JMETER_BIN", "jmeter"),
        data_dir=data_dir,
        api_key=os.environ.get("API_KEY") or None,
        allow_private_targets=_bool("ALLOW_PRIVATE_TARGETS", False),
        allowed_hosts=hosts,
        max_users=_int("MAX_USERS", 1000),
        max_duration_seconds=_int("MAX_DURATION_SECONDS", 3600),
        max_concurrent_tests=_int("MAX_CONCURRENT_TESTS", 2),
        max_upload_bytes=_int("MAX_UPLOAD_MB", 50) * 1024 * 1024,
    )
