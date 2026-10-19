"""Script design and debug replay through the API (JMeter is the test double in tests/fake_jmeter.py)."""
import io
import time
import urllib.request
import zipfile

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings
from tests.recorder import record_correlation_flow


def reset(target_url):
    """Forget every value the sample app issued while recording, so a replay must correlate."""
    urllib.request.urlopen(urllib.request.Request(f"{target_url}/reset", method="POST"), timeout=5).read()


def wait_debug(client, run_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/debug-runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.2)
    raise AssertionError("debug run did not finish")


def imported(client, target_url):
    r = client.post("/scripts/import-har", content=record_correlation_flow(target_url))
    assert r.status_code == 201, r.text
    reset(target_url)
    return r.json()


def test_debug_replay_passes_once_dynamic_values_are_correlated(client, target_url):
    script = imported(client, target_url)
    assert {c["variable"] for c in script["design"]["correlations"]} == {"csrf", "token", "id", "code", "XSRF_TOKEN"}
    assert script["validation"]["unresolved"] == {}

    started = client.post(f"/scripts/{script['id']}/debug-run")
    assert started.status_code == 201, started.text
    run = wait_debug(client, started.json()["id"])
    s = run["summary"]
    assert run["status"] == "completed", run
    assert s["verdict"] == "pass" and s["passed"] == 10 and s["first_failure"] is None, s["items"]
    assert set(s["variables"]) == {"csrf", "token", "id", "code", "XSRF_TOKEN"}
    assert not any(v["not_found"] for v in s["variables"].values())
    assert [r["id"] for r in client.get(f"/scripts/{script['id']}/debug-runs").json()] == [run["id"]]


def test_a_broken_correlation_is_pinpointed_and_explained(client, target_url):
    script = imported(client, target_url)
    rules = script["design"]["correlations"]
    for rule in rules:
        if rule["variable"] == "token":
            rule["expression"] = "$.nope"
    client.put(f"/scripts/{script['id']}/design", json={"correlations": rules}).raise_for_status()
    run = wait_debug(client, client.post(f"/scripts/{script['id']}/debug-run").json()["id"])
    s = run["summary"]
    assert s["verdict"] == "fail" and s["first_failure"] == 3
    failing = next(i for i in s["items"] if i["index"] == 3)
    assert failing["status"] == "401" and failing["diagnosis"]["where"] == "correlation"
    assert "${token}" in failing["diagnosis"]["cause"] and "#2" in failing["diagnosis"]["cause"]
    assert s["variables"]["token"]["not_found"]

    detail = client.get(f"/debug-runs/{run['id']}/samples/3").json()
    assert "Authorization: ********" in detail["request_headers"] and not detail["revealed"]
    revealed = client.get(f"/debug-runs/{run['id']}/samples/3", params={"reveal": True}).json()
    assert "Bearer token_NOT_FOUND" in revealed["request_headers"]
    assert client.get(f"/debug-runs/{run['id']}/samples/99").status_code == 404


def test_design_edits_are_validated(client, target_url):
    sid = imported(client, target_url)["id"]
    bad_source = {"correlations": [{"variable": "x", "source": 99, "extractor": "json", "expression": "$.a"}]}
    assert client.put(f"/scripts/{sid}/design", json=bad_source).status_code == 422
    clash = {"parameters": [{"name": "token", "type": "uuid"}]}
    r = client.put(f"/scripts/{sid}/design", json=clash)
    assert r.status_code == 422 and "token" in r.json()["detail"]
    assert client.put(f"/scripts/{sid}/design", json={"checks": [{"type": "json_path", "path": "x"}]}).status_code == 422
    unresolved = client.put(f"/scripts/{sid}/design", json={"replacements": [{"find": "book", "variable": "item"}]})
    assert unresolved.json()["validation"]["unresolved"] == {"item": [1]}


def test_data_files_settings_and_zip_export(client, target_url):
    sid = imported(client, target_url)["id"]
    r = client.post(f"/scripts/{sid}/files", params={"name": "users.csv"}, content=b"user name,password\nu1,p1\nu2,p2\n")
    assert r.status_code == 200, r.text
    files = r.json()["design"]["data_files"]
    assert files == [{"name": "users.csv", "columns": ["user_name", "password"], "sharing": "all", "recycle": True,
                      "stop_at_end": False, "first_line_header": True, "rows": 2}]
    files[0]["sharing"] = "user"
    settings = {"think_time": {"mode": "fixed", "seconds": 1.5}, "pacing": {"mode": "interval", "seconds": 20}}
    r = client.put(f"/scripts/{sid}/design", json={"data_files": files, "settings": settings,
                                                   "replacements": [{"find": "book", "variable": "user_name"}]})
    assert r.status_code == 200 and r.json()["design"]["data_files"][0]["rows"] == 2

    export = client.get(f"/scripts/{sid}/export-jmx")
    assert export.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(export.content)) as z:
        names = sorted(z.namelist())
        assert names[0].endswith(".jmx") and names[1] == "data/users.csv"
        plan = z.read(names[0]).decode()
        assert z.read("data/users.csv").startswith(b"user name,password")
    assert "shareMode.thread" in plan and "csrf=${csrf}&amp;item=${user_name}" in plan
    assert client.post(f"/scripts/{sid}/files", params={"name": "../x.csv"}, content=b"a\n1").status_code == 422
    assert client.delete(f"/scripts/{sid}/files/users.csv").json()["design"]["data_files"] == []


def test_debug_runs_are_checked_against_target_rules(tmp_path, target_url):
    raw = record_correlation_flow(target_url)
    with TestClient(create_app(make_settings(tmp_path, allow_private_targets=False))) as c:
        sid = c.post("/scripts/import-har", content=raw).json()["id"]
        r = c.post(f"/scripts/{sid}/debug-run")
        assert r.status_code == 422 and "private" in r.json()["detail"]


def test_deleting_a_script_removes_its_debug_runs(client, target_url, settings):
    script = imported(client, target_url)
    run = wait_debug(client, client.post(f"/scripts/{script['id']}/debug-run").json()["id"])
    run_dir = settings.runs_dir / run["id"]
    assert run_dir.exists()
    assert client.delete(f"/scripts/{script['id']}").status_code == 204
    assert client.get(f"/debug-runs/{run['id']}").status_code == 404 and not run_dir.exists()
