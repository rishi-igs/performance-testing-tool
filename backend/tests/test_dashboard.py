"""The overview (phase 4): running tests with live numbers, alerts and recent results."""
import time

from tests.conftest import wait_for


def quick(target_url, path="/fixed", **kw):
    return {"name": f"overview {path}", "target_url": f"{target_url}{path}", "users": 2, "duration_seconds": 2, **kw}


def test_the_overview_shows_running_tests_alerts_and_results(client, target_url):
    good = client.post("/tests", json=quick(target_url)).json()["id"]
    bad = client.post("/tests", json=quick(target_url, "/status/500")).json()["id"]
    wait_for(client, good)
    wait_for(client, bad)
    live = client.post("/tests", json=quick(target_url, "/slow?ms=50", duration_seconds=30, test_type="soak")).json()["id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        d = client.get("/dashboard").json()
        if d["running"] and d["running"][0]["requests"]:
            break
        time.sleep(0.3)
    [running] = d["running"]
    assert running["id"] == live and running["kind"] == "quick" and running["test_type"] == "soak"
    assert running["requests"] > 0 and running["users"] >= 1 and running["duration_seconds"] == 30
    recent = {r["id"]: r for r in d["recent"]}
    assert recent[good]["verdict"] == "pass" and recent[good]["p95_ms"] is not None
    assert recent[bad]["verdict"] == "fail" and recent[bad]["error_rate_percent"] == 100
    assert any(a["run"]["id"] == bad and a["severity"] == "critical" for a in d["alerts"])
    assert not any(a["run"]["id"] == good for a in d["alerts"])
    assert d["last_day"] == {"runs": 3, "passed": 1, "failed": 1, "running": 1}
    client.post(f"/tests/{live}/stop")
    wait_for(client, live)
    assert client.get("/dashboard").json()["running"] == []
