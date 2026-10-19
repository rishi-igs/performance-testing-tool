"""Script features against REAL Apache JMeter (skipped unless REAL_JMETER_BIN is set).

    REAL_JMETER_BIN=C:\\apache-jmeter-5.6.3\\bin\\jmeter.bat python -m pytest tests/test_real_jmeter.py

Each test records a flow against the local sample app, then replays it with JMeter, so the
generated extractors, data sets, functions, assertions and timers are checked by JMeter itself.
"""
import csv
import json
import os
import threading
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import script_builder
from app.services.test_executor import Outcome
from tests.conftest import make_settings
from tests.recorder import Recorder, record_correlation_flow
from tests.test_autocorrelate_api import raw_jmx, record_login, wait_job
from tests.test_debug_api import reset, wait_debug

REAL = os.environ.get("REAL_JMETER_BIN")
pytestmark = pytest.mark.skipif(not REAL, reason="set REAL_JMETER_BIN to run against real JMeter")


@pytest.fixture
def real(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, jmeter_bin=REAL, max_duration_seconds=300))) as c:
        yield c


def debug(client, sid):
    started = client.post(f"/scripts/{sid}/debug-run")
    assert started.status_code == 201, started.text
    return wait_debug(client, started.json()["id"], timeout=180)


def test_correlated_flow_replays_with_real_jmeter(real, target_url):
    script = real.post("/scripts/import-har", content=record_correlation_flow(target_url)).json()
    reset(target_url)
    run = debug(real, script["id"])
    s = run["summary"]
    assert run["status"] == "completed", run
    assert s["verdict"] == "pass", [(i["index"], i["status"], i["issues"]) for i in s["items"]]
    assert set(s["variables"]) == {"csrf", "token", "id", "code", "XSRF_TOKEN"}


def test_a_jmx_is_correlated_automatically_with_real_jmeter(real, target_url):
    raw = raw_jmx(record_correlation_flow(target_url))
    reset(target_url)
    sid = real.post("/scripts/import-jmx", content=raw).json()["id"]
    assert real.post(f"/scripts/{sid}/autocorrelate").status_code == 202
    job = wait_job(real, sid, timeout=600)
    assert job["status"] == "done" and job["message"] == "The last replay passed.", job
    assert set(job["correlations"]) == {"csrf", "token", "orders_id", "code", "XSRF_TOKEN"} and len(job["replays"]) == 2


def test_a_recorded_login_reads_users_csv_in_real_jmeter(real, target_url):
    sid = real.post("/scripts/import-har", content=record_login(target_url)).json()["id"]
    run = debug(real, sid)
    assert run["summary"]["verdict"] == "pass", run["summary"]["items"]
    assert real.get(f"/debug-runs/{run['id']}/samples/1").json()["request_body"] == "username=alice.w&password=S3cret-pass-1"
    sent_id = real.get(f"/debug-runs/{run['id']}/samples/2").json()["url"].split("requestId=")[1].split("&")[0]
    assert str(uuid.UUID(sent_id)) == sent_id, "a new UUID from ${__UUID()}"


def test_parameters_are_substituted_by_real_jmeter(real, target_url):
    rec = Recorder(target_url)
    rec.request("POST", "/echo", content_type="application/json",
                body='{"user":"USERX","city":"CITYX","color":"COLORX","num":"NUMX","uid":"UIDX","day":"DAYX","seq":"SEQX"}')
    sid = real.post("/scripts/import-har", content=rec.har()).json()["id"]
    real.post(f"/scripts/{sid}/files", params={"name": "users.csv"}, content=b"username\nalice\nbob\n").raise_for_status()
    design = {
        "parameters": [
            {"name": "city", "type": "list", "values": ["Paris"]},
            {"name": "color", "type": "list", "values": ["red", "blue"], "selection": "random"},
            {"name": "num", "type": "random_number", "minimum": 3, "maximum": 7},
            {"name": "uid", "type": "uuid"},
            {"name": "day", "type": "date", "format": "yyyy-MM-dd", "offset_days": 1},
            {"name": "seq", "type": "unique_number", "start": 41},
        ],
        "replacements": [{"find": "USERX", "variable": "username"}, {"find": "CITYX", "variable": "city"},
                         {"find": "COLORX", "variable": "color"}, {"find": "NUMX", "variable": "num"},
                         {"find": "UIDX", "variable": "uid"}, {"find": "DAYX", "variable": "day"},
                         {"find": "SEQX", "variable": "seq"}],
    }
    real.put(f"/scripts/{sid}/design", json=design).raise_for_status()
    run = debug(real, sid)
    assert run["summary"]["verdict"] == "pass", run["summary"]["items"]
    echoed = json.loads(real.get(f"/debug-runs/{run['id']}/samples/0").json()["response_body"])
    assert echoed["user"] == "alice" and echoed["city"] == "Paris" and echoed["color"] in ("red", "blue")
    assert 3 <= int(echoed["num"]) <= 7 and str(uuid.UUID(echoed["uid"])) == echoed["uid"]
    assert echoed["day"] in {(date.today() + timedelta(days=d)).isoformat() for d in (0, 1, 2)}
    assert echoed["seq"] == "41"


def test_checks_fail_with_a_readable_reason_in_real_jmeter(real, target_url):
    rec = Recorder(target_url)
    rec.request("GET", "/soft-error")
    rec.request("GET", "/fixed")
    sid = real.post("/scripts/import-har", content=rec.har()).json()["id"]
    checks = [{"item": 0, "type": "json_path", "path": "$.status", "expected": "ok"},
              {"item": 0, "type": "text", "value": "something failed"},
              {"item": 1, "type": "not_text", "value": "hello"},
              {"item": 1, "type": "size_max", "limit": 5},
              {"type": "duration", "limit": 60000}]
    real.put(f"/scripts/{sid}/design", json={"checks": checks}).raise_for_status()
    s = debug(real, sid)["summary"]
    first, second = s["items"]
    failed = lambda item: sorted(c["name"] for c in item["checks"] if c["failed"])  # noqa: E731
    assert failed(first) == ["Check: $.status"]
    assert failed(second) == ["Check: body at most 5 bytes", "Check: not_text hello"]
    assert first["diagnosis"]["where"] == "checks" and s["first_failure"] == 0


def test_pacing_and_think_time_with_real_jmeter(real, target_url):
    rec = Recorder(target_url)
    rec.request("GET", "/fixed")
    rec.request("GET", "/slow?ms=10", pause_ms=8000)          # idle gap: a second business function
    sid = real.post("/scripts/import-har", content=rec.har()).json()["id"]
    real.put(f"/scripts/{sid}/design", json={"settings": {
        "think_time": {"mode": "fixed", "seconds": 1}, "pacing": {"mode": "interval", "seconds": 4}}}).raise_for_status()

    state = real.app.state
    script = state.db.get_script(sid)
    from app.routers.scripts import evaluate
    _, view = evaluate(script)
    assert [t["name"] for t in view["transactions"]] == ["Fixed", "Slow"]
    built = script_builder.build(script, view, loops=2)
    done = threading.Event()
    result: dict = {}

    def finish(run_id: str, outcome: Outcome) -> None:
        result["outcome"] = outcome
        done.set()

    state.executor.launch("p_pacing", built.xml, duration_seconds=120, finish=finish, report=False)
    assert done.wait(180)
    rows = list(csv.DictReader(result["outcome"].paths["jtl"].open(encoding="utf-8")))
    starts = [int(r["timeStamp"]) for r in rows if r["label"] == "GET /fixed"]
    slow = [int(r["timeStamp"]) for r in rows if r["label"] == "GET /slow"]
    fixed_end = [int(r["timeStamp"]) + int(r["elapsed"]) for r in rows if r["label"] == "GET /fixed"]
    assert result["outcome"].exit_code == 0 and len(starts) == 2
    assert starts[1] - starts[0] >= 3800                      # pacing: an iteration starts every 4 s
    assert slow[0] - fixed_end[0] >= 950                      # think time between business functions


# ---- scenarios (the Controller) ------------------------------------------------------------

NO_THINK = {"think_time": {"mode": "ignore"}}


def scenario_run(client, cfg, timeout=180):
    from tests.conftest import wait_for
    created = client.post("/scenarios", json=cfg)
    assert created.status_code == 201, created.text
    run = client.post(f"/scenarios/{created.json()['id']}/run")
    assert run.status_code == 201, run.text
    rid = run.json()["id"]
    status = wait_for(client, rid, timeout=timeout)
    rows = list(csv.DictReader((client.app.state.settings.runs_dir / rid / "results.jtl").open(encoding="utf-8")))
    return status, rows


def two_step_script(client, target_url):
    rec = Recorder(target_url)
    rec.request("GET", "/fixed")
    rec.request("GET", "/slow?ms=30", pause_ms=8000)
    return client.post("/scripts/import-har", content=rec.har(), params={"name": "Two steps"}).json()["id"]


def test_rendezvous_and_ramp_down_with_real_jmeter(real, target_url):
    simple = two_step_script(real, target_url)
    cfg = {"name": "Rush", "schedule": {"ramp_up_seconds": 3, "duration_seconds": 4, "ramp_down_seconds": 4},
           "groups": [{"name": "Rushers", "script_id": simple, "users": 4, "settings": NO_THINK,
                       "schedule": {"ramp_up_seconds": 3, "duration_seconds": 6}},
                      {"name": "Leavers", "script_id": simple, "users": 4, "settings": NO_THINK}],
           "rendezvous": [{"name": "rush", "group": "Rushers", "transaction": "Slow", "timeout_seconds": 15}]}
    status, rows = scenario_run(real, cfg)
    assert status["status"] == "completed" and status["metrics"]["requests"]["error_rate_percent"] == 0
    rushers = sorted(int(r["timeStamp"]) for r in rows if r["label"] == "GET /slow" and r["threadName"].startswith("Rushers "))
    first = min(int(r["timeStamp"]) for r in rows if r["threadName"].startswith("Rushers "))
    # The first four /slow requests wait for the last user (it starts at about 2.25 s), then go together
    assert rushers[3] - rushers[0] < 400 and rushers[0] - first >= 1800, (rushers[:4], first)
    # Ramp-down: the Leavers' four cohorts stop one after another, not all at once
    ends = sorted(max(int(r["timeStamp"]) for r in rows if r["threadName"].startswith(f"Leavers ({n}/4) "))
                  for n in range(1, 5))
    assert ends[-1] - ends[0] >= 2000, ends


def test_goal_oriented_throughput_with_real_jmeter(real, target_url):
    simple = two_step_script(real, target_url)
    # The throughput timer spreads its events at random, so the steady window must hold enough of
    # them: at 12 s the count varied by about 8% and missed the 90% "reached" line about 1 run in 8.
    cfg = {"name": "Goal", "mode": "goal", "schedule": {"ramp_up_seconds": 2, "duration_seconds": 20},
           "goal": {"type": "transactions_per_second", "target": 4, "max_users": 6},
           "groups": [{"name": "Users", "script_id": simple, "settings": NO_THINK}]}
    status, _ = scenario_run(real, cfg)
    goal = status["metrics"]["goal"]
    assert goal["reached"] and 3.0 <= goal["actual"] <= 4.8, goal
    fixed = next(t for t in status["metrics"]["transactions"] if t["display"] == "Fixed")
    assert fixed["avg"] < 500, fixed      # the throughput timer's waits are not counted as response time
