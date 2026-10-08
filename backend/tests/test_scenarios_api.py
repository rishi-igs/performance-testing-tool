"""Scenarios through the API: create, validate, preview, run, results (JMeter is the test double)."""
import io
import time
import zipfile

from tests.conftest import wait_for
from tests.recorder import Recorder, record_correlation_flow
from tests.test_debug_api import reset


def imported(client, raw, name):
    r = client.post("/scripts/import-har", content=raw, params={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def simple_har(target_url):
    rec = Recorder(target_url)
    rec.request("GET", "/fixed")
    rec.request("GET", "/slow?ms=20", pause_ms=8000)
    return rec.har()


def scenario(flow, simple, **extra):
    return {"name": "Shop mix", "schedule": {"ramp_up_seconds": 1, "duration_seconds": 3, "ramp_down_seconds": 1},
            "groups": [{"name": "Shoppers", "script_id": flow, "users": 2,
                        "settings": {"think_time": {"mode": "ignore"}}},
                       {"name": "Browsers", "script_id": simple, "users": 2,
                        "settings": {"think_time": {"mode": "ignore"}}}],
            "sla": [{"transaction": "*", "metric": "error_rate", "limit": 5},
                    {"transaction": "Login", "metric": "p90", "limit": 5000}], **extra}


def test_create_preview_run_and_read_results(client, target_url):
    flow = imported(client, record_correlation_flow(target_url), "Flow")
    simple = imported(client, simple_har(target_url), "Simple")
    reset(target_url)
    r = client.post("/scenarios", json=scenario(flow, simple))
    assert r.status_code == 201, r.text
    sc = r.json()
    assert sc["total_users"] == 4 and sc["total_seconds"] == 5 and sc["runs"] == []
    assert [s["name"] for s in client.get("/scenarios").json()] == ["Shop mix"]

    preview = client.get(f"/scenarios/{sc['id']}/preview").json()
    assert set(preview["groups"]) == {"Shoppers", "Browsers"} and preview["hosts"] == ["127.0.0.1"]
    assert [tg["name"] for tg in preview["groups_plan"][0]["thread_groups"]] == ["Shoppers (1/2)", "Shoppers (2/2)"]

    run = client.post(f"/scenarios/{sc['id']}/run")
    assert run.status_code == 201, run.text
    rid = run.json()["id"]
    live = client.get(f"/tests/{rid}/status").json()
    assert live["status"] in ("running", "completed")
    final = wait_for(client, rid, timeout=60)
    assert final["status"] == "completed", final
    m = final["metrics"]
    assert m["verdict"] == "pass", m["warnings"]
    names = [t["display"] for t in m["transactions"]]
    assert "Login" in names and "Slow" in names
    assert {g["name"] for g in m["groups"]} == {"Shoppers", "Browsers"} and all(g["requests"] for g in m["groups"])
    assert m["requests"]["error_rate_percent"] == 0 and all(r["status"] == "pass" for r in m["sla"])

    record = client.get(f"/tests/{rid}").json()
    assert record["kind"] == "scenario" and record["scenario_id"] == sc["id"]
    assert [t["id"] for t in client.get("/tests", params={"kind": "scenario"}).json()] == [rid]
    assert client.get("/tests", params={"kind": "quick"}).json() == []
    timeline = client.get(f"/tests/{rid}/timeline", params={"bucket_seconds": 1}).json()
    assert set(timeline["groups"]) == {"Shoppers", "Browsers"} and "01 Page Form" in timeline["transactions"]
    assert client.get(f"/scenarios/{sc['id']}").json()["runs"][0]["id"] == rid
    assert client.get(f"/reports/{rid}/plan.jmx").status_code == 200


def test_sla_failures_fail_the_run(client, target_url):
    simple = imported(client, simple_har(target_url), "Simple")
    cfg = {"name": "Strict", "schedule": {"duration_seconds": 2},
           "groups": [{"name": "B", "script_id": simple, "settings": {"think_time": {"mode": "ignore"}}}],
           "sla": [{"transaction": "Slow", "metric": "avg", "limit": 1}]}
    sid = client.post("/scenarios", json=cfg).json()["id"]
    rid = client.post(f"/scenarios/{sid}/run").json()["id"]
    m = wait_for(client, rid, timeout=60)["metrics"]
    assert m["verdict"] == "fail" and m["sla"][0]["status"] == "fail"
    assert any(w["code"] == "sla" for w in m["warnings"])


def test_scenario_validation_and_limits(client, target_url):
    simple = imported(client, simple_har(target_url), "Simple")
    group = {"name": "B", "script_id": simple}
    assert client.post("/scenarios", json={"name": "x", "groups": [{"name": "B", "script_id": "s_missing"}]}).status_code == 422
    assert client.post("/scenarios", json={"name": "x", "groups": [group, group]}).status_code == 422
    assert client.post("/scenarios", json={"name": "x", "groups": [group],
                                           "rendezvous": [{"name": "r", "group": "nobody", "transaction": "Slow"}]}).status_code == 422
    too_many = client.post("/scenarios", json={"name": "big", "groups": [{**group, "users": 500}]}).json()["id"]
    r = client.post(f"/scenarios/{too_many}/run")
    assert r.status_code == 422 and "MAX_USERS" in r.json()["detail"]
    too_long = client.post("/scenarios", json={"name": "long", "groups": [group],
                                               "schedule": {"duration_seconds": 3600}}).json()["id"]
    assert client.post(f"/scenarios/{too_long}/run").status_code == 422


def test_update_export_and_delete(client, target_url):
    simple = imported(client, simple_har(target_url), "Simple")
    client.put(f"/scripts/{simple}/design", json={"parameters": [{"name": "city", "type": "list", "values": ["Oslo"]}]})
    sid = client.post("/scenarios", json={"name": "One", "groups": [{"name": "B", "script_id": simple}]}).json()["id"]
    updated = client.put(f"/scenarios/{sid}", json={"name": "One renamed", "groups": [{"name": "B", "script_id": simple, "users": 3}]})
    assert updated.status_code == 200 and updated.json()["total_users"] == 3
    export = client.get(f"/scenarios/{sid}/export-jmx")
    assert export.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(export.content)) as z:
        assert sorted(z.namelist()) == ["One-renamed.jmx", f"data/{simple}/city.csv"]
    assert client.delete(f"/scenarios/{sid}").status_code == 204
    assert client.get(f"/scenarios/{sid}").status_code == 404


def test_iterations_mode_runs_until_every_user_is_done(client, target_url):
    simple = imported(client, simple_har(target_url), "Simple")
    cfg = {"name": "Twice", "schedule": {"iterations": 2},
           "groups": [{"name": "B", "script_id": simple, "users": 2, "settings": {"think_time": {"mode": "ignore"}}}]}
    sid = client.post("/scenarios", json=cfg).json()["id"]
    started = time.time()
    m = wait_for(client, client.post(f"/scenarios/{sid}/run").json()["id"], timeout=60)["metrics"]
    assert m["requests"]["total_requests"] == 8 and time.time() - started < 30    # 2 users x 2 iterations x 2 requests
