"""Correlate automatically and parameterize at import, through the API (JMeter is the test double)."""
import io
import json
import time
import uuid
import zipfile
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.jmeter_plan_builder import build_recorded_plan
from tests.conftest import make_settings
from tests.recorder import Recorder, record_correlation_flow
from tests.test_debug_api import reset


def raw_jmx(har: bytes, variables: tuple[str, ...] = ()) -> bytes:
    """A .jmx that sends every recorded value as it was, as JMeter's own recorder makes it."""
    items = []
    for i, entry in enumerate(json.loads(har)["log"]["entries"]):
        req = entry["request"]
        headers = [[h["name"], h["value"]] for h in req["headers"] if h["name"].lower() not in ("cookie", "host")]
        items.append({"index": i, "label": f"{req['method']} {urlsplit(req['url']).path}", "method": req["method"],
                      "url": req["url"], "headers": headers, "body": (req.get("postData") or {}).get("text"),
                      "status": entry["response"]["status"]})
    return build_recorded_plan("Recorded with JMeter", [("Flow", items)], variables=variables).encode()


def wait_job(client, script_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/scripts/{script_id}/autocorrelate").json()
        if job["status"] != "running":
            return job
        time.sleep(0.3)
    raise AssertionError(f"automatic correlation of {script_id} still running after {timeout}s")


def test_a_jmx_is_correlated_by_replaying_it(client, target_url):
    raw = raw_jmx(record_correlation_flow(target_url))
    reset(target_url)                      # every recorded value is stale now, as in a new session
    imported = client.post("/scripts/import-jmx", content=raw, params={"name": "Recorded with JMeter"})
    assert imported.status_code == 201, imported.text
    script = imported.json()
    sid = script["id"]
    assert script["design"]["correlations"] == [] and any("Correlate automatically" in w for w in script["warnings"])
    assert client.get(f"/scripts/{sid}/autocorrelate").json() == {"status": "idle"}

    started = client.post(f"/scripts/{sid}/autocorrelate")
    assert started.status_code == 202 and started.json()["status"] == "running"
    assert client.post(f"/scripts/{sid}/autocorrelate").status_code == 409
    job = wait_job(client, sid)
    assert job["status"] == "done" and job["message"] == "The last replay passed.", job
    assert len(job["replays"]) == 2, "one replay finds the values, the second one proves them"

    detail = client.get(f"/scripts/{sid}").json()
    rules = {c["variable"]: c for c in detail["design"]["correlations"]}
    assert set(rules) == {"csrf", "token", "orders_id", "code", "XSRF_TOKEN"} == set(job["correlations"])
    assert (rules["token"]["source"], rules["token"]["expression"]) == (2, "$.token")
    assert (rules["orders_id"]["source"], rules["orders_id"]["expression"]) == (4, "$.order.id")
    assert rules["csrf"]["source"] == 0 and rules["code"]["scope"] == rules["XSRF_TOKEN"]["scope"] == "headers"
    assert all(r["origin"] == "auto" and r["replace"] and r["used_in"] for r in rules.values())
    assert detail["validation"]["unresolved"] == {}
    last = client.get(f"/debug-runs/{job['replays'][-1]}").json()["summary"]
    assert last["verdict"] == "pass" and last["passed"] == 10
    assert detail["suggestions"]["autocorrelate"]["correlations"] == job["correlations"], "the result is kept"

    assert client.post(f"/scripts/{sid}/autocorrelate").status_code == 202
    again = wait_job(client, sid)
    assert again["correlations"] == [] and len(again["replays"]) == 1 and again["message"] == "The last replay passed."


def test_variables_the_plan_defines_are_not_reused(client, target_url):
    raw = raw_jmx(record_correlation_flow(target_url), variables=("token",))      # the .jmx defines ${token} itself
    reset(target_url)
    sid = client.post("/scripts/import-jmx", content=raw).json()["id"]
    client.post(f"/scripts/{sid}/autocorrelate")
    job = wait_job(client, sid)
    assert "token_2" in job["correlations"] and "token" not in job["correlations"], job
    assert job["message"] == "The last replay passed."


def test_automatic_correlation_checks_targets_first(tmp_path, target_url):
    raw = raw_jmx(record_correlation_flow(target_url))
    with TestClient(create_app(make_settings(tmp_path, allow_private_targets=False))) as c:
        sid = c.post("/scripts/import-jmx", content=raw).json()["id"]
        r = c.post(f"/scripts/{sid}/autocorrelate")
        assert r.status_code == 422 and "private" in r.json()["detail"]
        assert c.get(f"/scripts/{sid}/autocorrelate").json() == {"status": "idle"}


def record_login(target_url, *, also=None):
    rec = Recorder(target_url)
    rec.request("GET", "/page/form")
    rec.request("POST", "/echo", body="username=alice.w&password=S3cret-pass-1",
                content_type="application/x-www-form-urlencoded")
    rec.request("GET", f"/fixed?requestId={uuid.uuid4()}&q=red+shoes")
    if also:
        rec.request("GET", also)
    return rec.har()


def test_a_recorded_login_and_generated_ids_are_parameterized_at_import(client, target_url):
    script = client.post("/scripts/import-har", content=record_login(target_url), params={"name": "Login"}).json()
    design = script["design"]
    assert design["data_files"] == [{"name": "users.csv", "columns": ["username", "password"], "sharing": "all",
                                     "recycle": True, "stop_at_end": False, "first_line_header": True, "rows": 1}]
    assert {"find": "alice.w", "variable": "username"} in design["replacements"]
    assert {"find": "S3cret-pass-1", "variable": "password"} in design["replacements"]
    assert [(p["name"], p["type"]) for p in design["parameters"]] == [("requestId", "uuid")]
    notes = " ".join(script["suggestions"]["auto"]["notes"])
    assert "users.csv" in notes and "${requestId}" in notes
    search = [s for s in script["suggestions"]["parameters"] if s["kind"] == "search"]
    assert search == [{"name": "q", "preview": "red shoes", "first_used": 2, "kind": "search", "value": "red shoes"}]

    exported = client.get(f"/scripts/{script['id']}/export-jmx")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as z:
        names = z.namelist()
        plan = z.read(next(n for n in names if n.endswith(".jmx"))).decode()
        assert z.read("data/users.csv") == b"username,password\nalice.w,S3cret-pass-1\n"
    assert "CSVDataSet" in plan and "username=${username}" in plan and "${__UUID()}" in plan
    assert "S3cret-pass-1" not in plan and "alice.w" not in plan


def test_a_login_whose_user_name_appears_elsewhere_is_left_alone(client, target_url):
    har = record_login(target_url, also="/fixed?note=alice.w")
    script = client.post("/scripts/import-har", content=har).json()
    assert script["design"]["data_files"] == [] and not any(r["variable"] == "username" for r in script["design"]["replacements"])
    assert "not parameterized automatically" in " ".join(script["suggestions"]["auto"]["notes"])
