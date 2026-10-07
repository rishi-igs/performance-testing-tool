from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FAKE_JMETER = str(Path(__file__).parent / "fake_jmeter.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def target_url():
    """Run the local sample application for the whole test session."""
    from tests.sample_app import app as sample

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(sample, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        jmeter_bin=FAKE_JMETER,
        data_dir=tmp_path / "data",
        api_key=None,
        allow_private_targets=True,
        allowed_hosts=(),
        max_users=50,
        max_duration_seconds=60,
        max_concurrent_tests=2,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def wait_for(client: TestClient, test_id: str, timeout: float = 30, headers=None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/tests/{test_id}/status", headers=headers).json()
        if data["status"] != "running":
            return data
        time.sleep(0.3)
    raise AssertionError(f"test {test_id} still running after {timeout}s")
