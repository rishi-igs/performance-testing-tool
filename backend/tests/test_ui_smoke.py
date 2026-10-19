"""Dashboard end to end: real backend over HTTP, real page scripts, a fake DOM (needs Node.js)."""
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from app.main import create_app
from tests.conftest import make_settings
from tests.recorder import record_correlation_flow
from tests.test_debug_api import reset

NODE = shutil.which("node")
SMOKE = Path(__file__).resolve().parents[2] / "frontend" / "tests" / "smoke.js"
pytestmark = pytest.mark.skipif(not NODE, reason="Node.js is not installed")


@pytest.fixture
def server(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(make_settings(tmp_path)), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


def test_script_workflow_in_the_dashboard(server, target_url):
    request = urllib.request.Request(f"{server}/scripts/import-har", data=record_correlation_flow(target_url),
                                     method="POST", headers={"Content-Type": "application/json"})
    script = json.loads(urllib.request.urlopen(request, timeout=30).read())
    reset(target_url)
    result = subprocess.run([NODE, str(SMOKE), server, script["id"]], capture_output=True, text=True, timeout=180)
    print(result.stdout, result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
