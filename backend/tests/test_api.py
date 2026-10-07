"""End-to-end tests: API -> plan generation -> executor -> results -> reports.

JMeter itself is replaced by tests/fake_jmeter.py, which reads the generated .jmx and
sends real HTTP requests to the local sample app.
"""
import os
import signal
import time

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings, wait_for


def body(target_url, path="/fixed", **kw):
    return {"name": "api test", "target_url": f"{target_url}{path}", "users": 3,
            "duration_seconds": 3, **kw}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "auth_required": False}


def test_successful_run_end_to_end(client, target_url):
    r = client.post("/tests", json=body(target_url, think_time_ms=50, headers={"Authorization": "Bearer s3cret", "X-Trace": "1"}))
    assert r.status_code == 201
    test_id = r.json()["id"]

    status = wait_for(client, test_id)
    assert status["status"] == "completed", status
    m = status["metrics"]
    assert m["total_requests"] > 10 and m["error_rate_percent"] == 0
    assert m["verdict"] == "pass" and m["response_time_ms"]["p95"] >= 0
    assert m["status_codes"] == {"200": m["total_requests"]}

    # stored record: secrets masked, metadata present
    rec = client.get(f"/tests/{test_id}").json()
    assert rec["config"]["headers"] == {"Authorization": "********", "X-Trace": "1"}
    assert "s3cret" not in client.get(f"/tests/{test_id}").text
    assert rec["created_by"] and rec["started_at"] and rec["finished_at"]
    assert any(t["id"] == test_id for t in client.get("/tests").json())

    # reports and exports
    assert client.get(f"/tests/{test_id}/timeline").json()["points"]
    metrics = client.get(f"/reports/{test_id}/metrics.json")
    assert metrics.status_code == 200 and metrics.json()["metrics"]["total_requests"] == m["total_requests"]
    assert client.get(f"/reports/{test_id}/results.csv").text.startswith("timeStamp,")
    jmx = client.get(f"/reports/{test_id}/plan.jmx")
    assert jmx.status_code == 200 and "<jmeterTestPlan" in jmx.text
    html = client.get(f"/reports/{test_id}", follow_redirects=True)
    assert html.status_code == 200 and "report" in html.text.lower()


def test_failing_endpoint_gets_critical_verdict_and_readable_warning(client, target_url):
    test_id = client.post("/tests", json=body(target_url, "/status/500")).json()["id"]
    m = wait_for(client, test_id)["metrics"]
    assert m["error_rate_percent"] == 100 and m["verdict"] == "fail"
    codes = {w["code"] for w in m["warnings"]}
    assert {"error_rate", "http_5xx", "assertion_failures"} <= codes


def test_expected_status_codes_are_respected(client, target_url):
    test_id = client.post("/tests", json=body(target_url, "/status/404", expected_status_codes=[404])).json()["id"]
    m = wait_for(client, test_id)["metrics"]
    assert m["error_rate_percent"] == 0 and m["verdict"] == "pass"


def test_connection_failures_detected(client):
    test_id = client.post("/tests", json={"name": "down", "target_url": "http://127.0.0.1:9/x",
                                          "users": 2, "duration_seconds": 2}).json()["id"]
    m = wait_for(client, test_id)["metrics"]
    assert m["connection_failures"] > 0 and m["verdict"] == "fail"


def test_stop_running_test(client, target_url):
    test_id = client.post("/tests", json=body(target_url, "/slow?ms=100", duration_seconds=60)).json()["id"]
    time.sleep(1.5)
    live = client.get(f"/tests/{test_id}/status").json()
    assert live["status"] == "running" and live["metrics"]["total_requests"] > 0   # live results work

    t0 = time.time()
    assert client.post(f"/tests/{test_id}/stop").status_code == 200
    final = wait_for(client, test_id, timeout=20)
    assert final["status"] == "stopped" and time.time() - t0 < 15
    assert final["metrics"]["total_requests"] > 0                      # partial results kept
    assert client.get(f"/reports/{test_id}", follow_redirects=True).status_code == 200
    assert client.post(f"/tests/{test_id}/stop").status_code == 409     # already finished


def test_input_validation_and_limits(client, target_url):
    assert client.post("/tests", json={"name": "x", "target_url": "nope"}).status_code == 422
    assert client.post("/tests", json=body(target_url, users=500)).status_code == 422
    assert client.post("/tests", json=body(target_url, duration_seconds=99999)).status_code == 422
    assert client.get("/tests/t_missing").status_code == 404


def test_private_targets_blocked_by_default(tmp_path, target_url):
    settings = make_settings(tmp_path, allow_private_targets=False)
    with TestClient(create_app(settings)) as c:
        r = c.post("/tests", json=body(target_url))
        assert r.status_code == 422 and "private" in r.json()["detail"]
        r = c.post("/tests", json={"name": "m", "target_url": "http://169.254.169.254/"})
        assert r.status_code == 422


def test_api_key_required_when_configured(tmp_path, target_url):
    with TestClient(create_app(make_settings(tmp_path, api_key="k3y"))) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/tests").status_code == 401
        assert c.post("/tests", json=body(target_url)).status_code == 401
        assert c.get("/tests", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/tests", headers={"X-API-Key": "k3y"}).status_code == 200
        assert c.get("/tests", cookies={"api_key": "k3y"}).status_code == 200


def test_concurrency_limit(tmp_path, target_url):
    with TestClient(create_app(make_settings(tmp_path, max_concurrent_tests=1))) as c:
        first = c.post("/tests", json=body(target_url, duration_seconds=5)).json()["id"]
        assert c.post("/tests", json=body(target_url)).status_code == 429
        c.post(f"/tests/{first}/stop")
        wait_for(c, first, timeout=20)
        assert c.post("/tests", json=body(target_url, duration_seconds=1)).status_code == 201


def test_missing_jmeter_gives_actionable_error(tmp_path, target_url):
    with TestClient(create_app(make_settings(tmp_path, jmeter_bin="/no/such/jmeter"))) as c:
        test_id = c.post("/tests", json=body(target_url)).json()["id"]
        status = wait_for(c, test_id)
        assert status["status"] == "failed" and "JMETER_BIN" in status["error"]


def test_report_path_traversal_is_blocked(client, target_url):
    test_id = client.post("/tests", json=body(target_url, duration_seconds=1)).json()["id"]
    wait_for(client, test_id)
    for evil in ("../results.jtl", "..%2Fresults.jtl", "../../perf_tool.sqlite3"):
        assert client.get(f"/reports/{test_id}/html/{evil}").status_code == 404


def test_interrupted_tests_are_marked_failed_on_restart(tmp_path, target_url):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as c:
        test_id = c.post("/tests", json=body(target_url, duration_seconds=30)).json()["id"]
        # simulate a crash: leave the row 'running' without stopping the process cleanly
        time.sleep(0.5)
        orphan = c.app.state.executor._runs[test_id].proc
        c.app.state.executor._runs.clear()
        c.app.state.db.update_test(test_id, status="running")
    with TestClient(create_app(settings)) as c2:
        rec = c2.get(f"/tests/{test_id}").json()
        assert rec["status"] == "failed" and "restarted" in rec["error"]
    os.killpg(os.getpgid(orphan.pid), signal.SIGKILL)   # clean up the simulated orphan
