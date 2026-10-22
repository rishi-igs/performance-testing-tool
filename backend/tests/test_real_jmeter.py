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
from tests.conftest import make_settings, wait_for
from tests.recorder import Recorder, record_correlation_flow, record_demo_flow
from tests.test_autocorrelate_api import raw_jmx, record_login, wait_job
from tests.test_debug_api import reset, wait_debug

REAL = os.environ.get("REAL_JMETER_BIN")
pytestmark = pytest.mark.skipif(not REAL, reason="set REAL_JMETER_BIN to run against real JMeter")


@pytest.fixture
def real(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, jmeter_bin=REAL, max_duration_seconds=300))) as c:
        yield c


def debug(client, sid, **params):
    started = client.post(f"/scripts/{sid}/debug-run", params=params)
    assert started.status_code == 201, started.text
    return wait_debug(client, started.json()["id"], timeout=180)


def test_phase_3_exit_criterion_with_real_jmeter(real, target_url):
    """A recorded sign-in and order flow passes validation and a debug run, every check green."""
    sid = real.post("/scripts/import-har", content=record_demo_flow(target_url)).json()["id"]
    report = real.get(f"/scripts/{sid}/validation").json()
    assert report["status"] == "pass" and not report["unresolved_values"] and not report["unresolved_variables"]
    reset(target_url)
    run = debug(real, sid)
    s = run["summary"]
    assert s["verdict"] == "pass", [(i["index"], i["status"], i["issues"]) for i in s["items"]]
    assert s["passed"] == 8 and all(not c["failed"] for i in s["items"] for c in i["checks"])
    assert {"token", "csrf", "XSRF_TOKEN"} <= set(s["variables"])


def test_a_replay_with_two_users_and_two_iterations_in_real_jmeter(real, target_url):
    sid = real.post("/scripts/import-har", content=record_correlation_flow(target_url)).json()["id"]
    reset(target_url)
    s = debug(real, sid, users=2, iterations=2)["summary"]
    assert s["verdict"] == "pass", [(i["index"], i["issues"]) for i in s["items"] if i["failed_runs"]]
    assert all(i["runs"] == 4 for i in s["items"])


def users_at(rows, second, t0):
    """Most threads JMeter reported running during one second of the run."""
    seen = [int(r["allThreads"]) for r in rows if second <= (int(r["timeStamp"]) - t0) / 1000 < second + 1]
    return max(seen, default=0)


def test_a_spike_profile_with_real_jmeter(real, target_url):
    cfg = {"name": "spike", "target_url": f"{target_url}/slow?ms=20", "test_type": "spike", "duration_seconds": 14,
           "profile": [{"time": 0, "users": 2}, {"time": 4, "users": 8}, {"time": 9, "users": 2}],
           "thresholds": {"max_p95_ms": None}}
    created = real.post("/tests", json=cfg)
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    status = wait_for(real, tid, timeout=120)
    assert status["status"] == "completed" and status["metrics"]["peak_users"] == 8
    rows = list(csv.DictReader((real.app.state.settings.runs_dir / tid / "results.jtl").open(encoding="utf-8")))
    t0 = min(int(r["timeStamp"]) for r in rows)
    assert users_at(rows, 2, t0) == 2 and users_at(rows, 6, t0) == 8 and users_at(rows, 11, t0) == 2


def test_a_breakpoint_test_stops_itself_with_real_jmeter(real, target_url):
    cfg = {"name": "break", "target_url": f"{target_url}/status/500", "test_type": "breakpoint", "duration_seconds": 60,
           "profile": [{"time": 0, "users": 1}, {"time": 1, "users": 5, "ramp": 20}],
           "stop_when": {"error_rate_percent": 50, "for_seconds": 3}}
    tid = real.post("/tests", json=cfg).json()["id"]
    status = wait_for(real, tid, timeout=90)
    m = status["metrics"]
    assert status["status"] == "completed" and m["breaking_point"]["error_rate_percent"] > 50
    assert m["duration_seconds"] < 40, "the run stopped long before its 60 s"
    assert_jmeter_stopped(real, tid)


def assert_jmeter_stopped(client, test_id):
    """JMeter itself has ended: its results file no longer grows, and covers only the run until the stop."""
    jtl = client.app.state.settings.runs_dir / test_id / "results.jtl"
    size = jtl.stat().st_size
    import time
    time.sleep(4)
    assert jtl.stat().st_size == size, "JMeter is still writing results after the run ended"
    stamps = [int(r["timeStamp"]) for r in csv.DictReader(jtl.open(encoding="utf-8"))]
    assert (max(stamps) - min(stamps)) / 1000 < 30, "results continue long after the stop"


def test_stopping_a_run_stops_jmeter(real, target_url):
    tid = real.post("/tests", json={"name": "stop", "target_url": f"{target_url}/slow?ms=20", "users": 3,
                                    "duration_seconds": 120}).json()["id"]
    import time
    time.sleep(6)
    assert real.post(f"/tests/{tid}/stop").status_code == 200
    t0 = time.time()
    status = wait_for(real, tid, timeout=40)
    assert status["status"] == "stopped" and time.time() - t0 < 10, "StopTestNow ends JMeter without waiting for a kill"
    assert status["metrics"]["total_requests"] > 0
    assert_jmeter_stopped(real, tid)


def test_a_scenario_follows_a_profile_and_reads_a_semicolon_file_in_real_jmeter(real, target_url):
    rec = Recorder(target_url)
    rec.request("POST", "/echo", body='{"city": "CITYX"}', content_type="application/json")
    sid = real.post("/scripts/import-har", content=rec.har()).json()["id"]
    real.post(f"/scripts/{sid}/files", params={"name": "cities.csv", "delimiter": ";"},
              content=b"city;country\nParis;FR\n").raise_for_status()
    real.put(f"/scripts/{sid}/design", json={"replacements": [{"find": "CITYX", "variable": "city"}]}).raise_for_status()
    run = debug(real, sid)
    assert json.loads(real.get(f"/debug-runs/{run['id']}/samples/0").json()["response_body"])["city"] == "Paris"
    cfg = {"name": "Waves", "schedule": {"duration_seconds": 12, "profile": [
               {"time": 0, "users": 100, "ramp": 2}, {"time": 5, "users": 0}, {"time": 7, "users": 100}]},
           "groups": [{"name": "Wave", "script_id": sid, "users": 3, "settings": NO_THINK}]}
    status, rows = scenario_run(real, cfg)
    assert status["status"] == "completed", status
    starts = lambda name: [int(r["timeStamp"]) for r in rows if r["threadName"].startswith(name)]  # noqa: E731
    first, again = starts("Wave · step 1 "), starts("Wave · step 3 ")
    assert first and again, sorted({r["threadName"] for r in rows})
    assert 1500 <= min(again) - max(first) <= 3000, (max(first), min(again))   # the 2 s pause between the waves
    assert max(first) - min(first) >= 3500 and max(again) - min(again) >= 3500  # each wave ran about 5 s


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
    sample = f"/debug-runs/{run['id']}/samples/1"
    assert real.get(sample).json()["request_body"] == "username=alice.w&password=********"      # masked by default
    assert real.get(sample, params={"reveal": True}).json()["request_body"] == "username=alice.w&password=S3cret-pass-1"
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
