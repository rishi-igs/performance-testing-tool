"""Scheduled runs (when they fall due, how they start) and trends across a scenario's runs."""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import analysis
from app.services import scheduler as scheduling
from app.services.scheduler import next_run, zone_of
from tests.conftest import wait_for
from tests.test_scenarios_api import imported, simple_har

UTC = timezone.utc


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_next_run_once_daily_and_weekly():
    once = {"repeat": "once", "at": "2026-10-09T09:00", "timezone": "UTC"}
    assert next_run(once, at("2026-10-08T12:00")) == at("2026-10-09T09:00")
    assert next_run(once, at("2026-10-09T09:00")) is None, "strictly after: a past one-off has no next run"
    daily = {"repeat": "daily", "time": "09:00", "timezone": "UTC"}
    assert next_run(daily, at("2026-10-08T08:59")) == at("2026-10-08T09:00")
    assert next_run(daily, at("2026-10-08T09:00")) == at("2026-10-09T09:00")
    weekly = {"repeat": "weekly", "time": "18:30", "days": [0, 4], "timezone": "UTC"}   # Monday and Friday
    assert at("2026-10-08T00:00").weekday() == 3                                          # a Thursday
    assert next_run(weekly, at("2026-10-08T12:00")) == at("2026-10-09T18:30")
    assert next_run(weekly, at("2026-10-09T19:00")) == at("2026-10-12T18:30")


def test_local_time_uses_the_saved_offset_without_a_zone_database(monkeypatch):
    monkeypatch.setattr(scheduling, "has_zone_database", lambda: False)
    cfg = {"repeat": "daily", "time": "09:00", "timezone": "Asia/Kolkata", "utc_offset_minutes": 330}
    assert zone_of(cfg)[1] == "offset"
    assert next_run(cfg, at("2026-10-08T00:00")) == at("2026-10-08T03:30")
    once = {"repeat": "once", "at": "2026-10-09T09:00", "timezone": "America/New_York", "utc_offset_minutes": -240}
    assert next_run(once, at("2026-10-08T00:00")) == at("2026-10-09T13:00")


@pytest.mark.skipif(not scheduling.has_zone_database(), reason="no time-zone database here (pip install tzdata)")
def test_named_time_zones_follow_daylight_saving():
    cfg = {"repeat": "daily", "time": "09:00", "timezone": "Europe/Paris", "utc_offset_minutes": 120}
    assert zone_of(cfg)[1] == "named"
    assert next_run(cfg, at("2026-10-20T00:00")) == at("2026-10-20T07:00")      # summer time, UTC+2
    assert next_run(cfg, at("2026-10-26T00:00")) == at("2026-10-26T08:00")      # winter time from 25 October


def scenario_id(client, target_url, users=1):
    script = imported(client, simple_har(target_url), "Simple")
    body = {"name": "Nightly", "schedule": {"ramp_up_seconds": 0, "duration_seconds": 2},
            "groups": [{"name": "B", "script_id": script, "users": users, "settings": {"think_time": {"mode": "ignore"}}}],
            "sla": [{"transaction": "*", "metric": "error_rate", "limit": 5}]}
    r = client.post("/scenarios", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def schedule(client, sid, **cfg):
    return client.post("/schedules", json={"scenario_id": sid, "timezone": "UTC", **cfg})


def test_a_due_schedule_starts_one_run(client, target_url):
    sid = scenario_id(client, target_url)
    when = (datetime.now(UTC) + timedelta(hours=1)).replace(second=0, microsecond=0)
    r = schedule(client, sid, repeat="once", at=when.strftime("%Y-%m-%dT%H:%M"))
    assert r.status_code == 201, r.text
    sched = r.json()
    assert sched["next_run_at"] == when.isoformat() and sched["project_id"] == "p_default" and sched["zone_mode"] == "named"

    scheduler = client.app.state.scheduler
    assert scheduler.tick() == [], "not due yet"
    started = scheduler.tick(now=when + timedelta(seconds=20))
    assert len(started) == 1
    assert scheduler.tick(now=when + timedelta(seconds=40)) == [], "a schedule is claimed: never started twice"
    after = client.get("/schedules", params={"scenario_id": sid}).json()[0]
    assert after["last_run_id"] == started[0] and after["next_run_at"] is None and after["last_error"] is None
    run = client.get(f"/tests/{started[0]}").json()
    assert run["created_by"] == f"schedule:{sched['id']}" and run["scenario_id"] == sid and run["kind"] == "scenario"
    assert wait_for(client, started[0], timeout=60)["status"] == "completed"


def test_daily_runs_move_on_and_missed_runs_are_skipped(client, target_url):
    sid = scenario_id(client, target_url)
    sched = schedule(client, sid, repeat="daily", time="02:00").json()
    due = datetime.fromisoformat(sched["next_run_at"])
    assert due.hour == 2 and due.minute == 0 and due > datetime.now(UTC)

    scheduler = client.app.state.scheduler
    assert scheduler.tick(now=due + timedelta(minutes=30)) == []
    after = client.get("/schedules").json()[0]
    assert "Skipped" in after["last_error"] and after["last_run_id"] is None
    assert datetime.fromisoformat(after["next_run_at"]) == due + timedelta(days=1)


def test_schedules_record_why_a_run_did_not_start(client, target_url):
    sid = scenario_id(client, target_url, users=60)                 # the test settings allow 50 users
    sched = schedule(client, sid, repeat="weekly", time="23:15", days=[5, 6]).json()
    assert sched["config"]["days"] == [5, 6]
    due = datetime.fromisoformat(sched["next_run_at"])
    assert due.weekday() in (5, 6)
    assert client.app.state.scheduler.tick(now=due + timedelta(seconds=5)) == []
    after = client.get(f"/schedules?scenario_id={sid}").json()[0]
    assert after["last_error"].startswith("Not started: The scenario needs 60 users")
    assert datetime.fromisoformat(after["next_run_at"]) > due, "it tries again next time"


def test_schedule_validation_disable_and_delete(client, target_url):
    sid = scenario_id(client, target_url)
    past = schedule(client, sid, repeat="once", at="2020-01-01T00:00")
    assert past.status_code == 422 and "already passed" in past.json()["detail"]
    assert schedule(client, sid, repeat="weekly", time="09:00").status_code == 422
    assert schedule(client, sid, repeat="daily", time="25:00").status_code == 422
    assert schedule(client, sid, repeat="daily", time="09:00", timezone="../etc").status_code == 422
    assert schedule(client, "c_missing", repeat="daily", time="09:00").status_code == 422

    sched = schedule(client, sid, repeat="daily", time="09:00", enabled=False).json()
    assert sched["next_run_at"] is None
    assert client.app.state.scheduler.tick(now=datetime.now(UTC) + timedelta(days=2)) == []
    body = {"scenario_id": sid, "repeat": "daily", "time": "09:00", "timezone": "UTC", "enabled": True}
    enabled = client.put(f"/schedules/{sched['id']}", json=body).json()
    assert enabled["next_run_at"] is not None
    assert client.put(f"/schedules/{sched['id']}", json=body | {"scenario_id": "c_other"}).status_code == 422

    second = schedule(client, sid, repeat="daily", time="10:00").json()
    assert client.delete(f"/schedules/{second['id']}").status_code == 204
    assert client.delete(f"/scenarios/{sid}").status_code == 204
    assert client.get("/schedules").json() == [], "a scenario's schedules go with it"


def test_trends_and_the_baseline(client, target_url):
    sid = scenario_id(client, target_url)
    runs = []
    for _ in range(2):
        rid = client.post(f"/scenarios/{sid}/run").json()["id"]
        assert wait_for(client, rid, timeout=60)["status"] == "completed"
        runs.append(rid)

    t = client.get(f"/scenarios/{sid}/trends").json()
    assert [r["id"] for r in t["runs"]] == runs, "oldest first"
    assert t["baseline_run_id"] is None and t["vs_baseline"] is None
    for row in t["runs"]:
        assert row["requests"] > 0 and row["verdict"] == "pass" and row["sla_failed"] == 0, row
        assert row["sla_total"] == len(t["transactions"]), ("an SLA on every business function is one check each", row)
    slow = next(x for x in t["transactions"] if x["name"] == "Slow")
    assert len(slow["points"]) == 2 and all(p["p90"] >= 20 for p in slow["points"])

    assert client.put(f"/scenarios/{sid}/baseline", json={"run_id": "t_missing"}).status_code == 422
    assert client.put(f"/scenarios/{sid}/baseline", json={"run_id": runs[0]}).status_code == 200
    assert client.get(f"/scenarios/{sid}").json()["baseline_run_id"] == runs[0]
    vs = client.get(f"/scenarios/{sid}/trends").json()["vs_baseline"]
    assert vs["baseline_run_id"] == runs[0] and vs["run_id"] == runs[1]
    assert {r["name"] for r in vs["rows"]} >= {"Fixed", "Slow"}
    assert client.put(f"/scenarios/{sid}/baseline", json={"run_id": None}).status_code == 200
    assert client.get(f"/scenarios/{sid}/trends").json()["baseline_run_id"] is None


def test_trend_regressions_follow_the_comparison_rule():
    def run(rid, p90, errors):
        tx = {"name": "01 Pay", "display": "Pay", "avg": p90, "p90": p90, "p95": p90, "tps": 1.0, "error_rate_percent": errors}
        return {"id": rid, "created_at": "2026-10-08T00:00:00+00:00", "status": "completed", "config": {"total_users": 5},
                "summary": {"verdict": "pass", "requests": {"total_requests": 10}, "sla": [], "transactions": [tx]}}

    base, ok, slow, failing = run("a", 100, 0), run("b", 140, 0), run("c", 300, 0), run("d", 100, 2.5)
    assert analysis.trends([base, ok], base)["vs_baseline"]["regressions"] == 0, "40 ms slower is under the 50 ms floor"
    assert analysis.trends([base, slow], base)["vs_baseline"]["rows"][0]["verdict"] == "slower"
    assert analysis.trends([base, failing], base)["vs_baseline"]["rows"][0]["verdict"] == "more errors"
    assert analysis.trends([base], base)["vs_baseline"] is None, "the baseline is not compared with itself"
