"""Scenario plans: schedules, groups, rendezvous, goals (no JMeter needed)."""
import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from app.models.scenario import ScenarioConfig, Schedule
from app.models.script import FilterRules, ScriptDesign
from app.services.har_parser import parse_har
from app.services.jmx_importer import parse_jmx
from app.services.scenario_analyzer import evaluate_sla, group_of, label_stats, scenario_summary, split
from app.services.result_analyzer import Sample
from app.services.scenario_builder import ScenarioError, build_scenario, cohorts, planned_users
from tests.recordings import SHOP_HAR, SHOP_JMX


def script(sid, raw=SHOP_HAR, source="har", design=None):
    parsed = parse_har(raw) if source == "har" else parse_jmx(raw)
    return {"id": sid, "name": sid, "source": source, "items": parsed["items"], "variables": parsed["variables"],
            "rules": FilterRules().model_dump(), "overrides": {}, "original": raw if source == "jmx" else None,
            "design": ScriptDesign(**(design or {})).model_dump()}


def build(cfg, scripts=None, files=None, engines=1):
    scripts = scripts or {"s1": script("s1"), "s2": script("s2")}
    plan = build_scenario(ScenarioConfig(**cfg), scripts, files or {}, engines=engines)
    return plan, ET.fromstring(plan.xml.encode())


def thread_groups(root):
    plan_tree = root.find("hashTree/hashTree")
    kids = list(plan_tree)
    return [(el, kids[i + 1]) for i, el in enumerate(kids) if el.tag == "ThreadGroup"]


def props(el):
    return {c.get("name"): c.text for c in el.iter() if c.get("name")}


def test_cohorts_follow_ramp_up_hold_and_ramp_down():
    out = cohorts(10, Schedule(start_delay_seconds=5, ramp_up_seconds=20, duration_seconds=60, ramp_down_seconds=10), "G")
    assert len(out) == 10 and sum(c.users for c in out) == 10
    assert [c.delay for c in out[:3]] == [5, 7, 9] and all(c.ramp == 2 for c in out)
    stops = [c.delay + c.duration for c in out]
    assert stops == [86, 87, 88, 89, 90, 91, 92, 93, 94, 95]    # 5 + 20 + 60, then one cohort per second
    single = cohorts(4, Schedule(ramp_up_seconds=8, duration_seconds=30), "G")
    assert [(c.users, c.delay, c.ramp, c.duration) for c in single] == [(4, 0, 8, 38)]
    assert cohorts(3, Schedule(iterations=5), "G")[0].loops == 5
    with pytest.raises(ValidationError):
        Schedule(iterations=5, ramp_down_seconds=3)


def test_planned_users_draw_the_schedule():
    cfg = ScenarioConfig(name="s", schedule={"ramp_up_seconds": 10, "duration_seconds": 10, "ramp_down_seconds": 10},
                         groups=[{"name": "A", "script_id": "s1", "users": 10}])
    curve = planned_users(cfg, step=5)["groups"]["A"]
    assert [p["users"] for p in curve] == [1, 6, 10, 10, 10, 5, 0]


def test_each_group_gets_its_own_thread_groups_with_the_scripts_config():
    plan, root = build({"name": "Mix", "schedule": {"ramp_up_seconds": 4, "duration_seconds": 30, "ramp_down_seconds": 3},
                        "groups": [{"name": "Buyers", "script_id": "s1", "users": 3},
                                   {"name": "Lookers", "script_id": "s2", "users": 2,
                                    "schedule": {"start_delay_seconds": 10, "duration_seconds": 20}}]})
    tgs = thread_groups(root)
    assert [el.get("testname") for el, _ in tgs] == ["Buyers (1/3)", "Buyers (2/3)", "Buyers (3/3)", "Lookers"]
    lookers = props(tgs[3][0])
    assert lookers["ThreadGroup.num_threads"] == "2" and lookers["ThreadGroup.delay"] == "10"
    assert lookers["ThreadGroup.duration"] == "20" and lookers["ThreadGroup.scheduler"] == "true"
    for _, tree in tgs:   # config elements are inside each thread group, not shared at plan level
        tags = [el.tag for el in tree if el.tag != "hashTree"]
        assert tags[:3] == ["ConfigTestElement", "CookieManager", "HeaderManager"] and tags.count("TransactionController") == 3
    assert [el.tag for el in root.find("hashTree/hashTree") if el.tag != "hashTree"] == ["ThreadGroup"] * 4
    assert root.find(".//ResultCollector") is None
    assert plan.total_users == 5 and plan.duration_seconds == 37 and plan.hosts == ["shop.test"]
    assert [g["transactions"] for g in plan.groups][0] == ["Home", "Auth Login", "Products Search"]
    assert all(props(tc)["TransactionController.includeTimers"] == "false" for tc in root.iter("TransactionController"))


def test_rendezvous_waits_before_the_first_request_of_a_business_function():
    plan, root = build({"name": "R", "groups": [{"name": "A", "script_id": "s1", "users": 8}],
                        "rendezvous": [{"name": "peak", "group": "A", "transaction": "auth login", "percent": 50,
                                        "timeout_seconds": 20},
                                       {"name": "nowhere", "group": "A", "transaction": "Checkout"}]})
    [(_, tree)] = thread_groups(root)
    login_tc = next(i for i, el in enumerate(tree) if el.get("testname") == "02 Auth Login")
    sampler_tree = list(tree[login_tc + 1])[1]
    assert props(sampler_tree[0]) == {"groupSize": "4", "timeoutInMs": "20000"}
    assert sampler_tree[0].get("testname") == "Rendezvous: peak"
    assert any("no business function Checkout" in w for w in plan.warnings)


def test_goal_scenarios_throttle_with_precise_throughput_timers():
    goal = {"name": "G", "mode": "goal", "schedule": {"ramp_up_seconds": 10, "duration_seconds": 60},
            "groups": [{"name": "A", "script_id": "s1", "users": 3}, {"name": "B", "script_id": "s2", "users": 1}]}
    plan, root = build({**goal, "goal": {"type": "hits_per_second", "target": 40, "max_users": 20}})
    (a_el, a_tree), (b_el, b_tree) = thread_groups(root)
    assert props(a_el)["ThreadGroup.num_threads"] == "15" and props(b_el)["ThreadGroup.num_threads"] == "5"
    assert a_tree[0].tag == "PreciseThroughputTimer" and props(a_tree[0])["throughput"] == "30.0"
    assert props(a_tree[0])["duration"] == "70"
    _, root = build({**goal, "goal": {"type": "transactions_per_second", "target": 12, "max_users": 8}})
    (_, a_tree), _ = thread_groups(root)
    assert a_tree[0].tag == "TestAction" and a_tree[0].get("testname") == "Goal pacing"
    assert props(a_tree[1].find("PreciseThroughputTimer"))["throughput"] == "3.0"   # 12 * 3/4 / 3 functions
    with pytest.raises(ValidationError):
        ScenarioConfig(**{**goal, "goal": None})


def test_data_files_variables_and_network_speed():
    s1 = script("s1", design={"parameters": [{"name": "city", "type": "list", "values": ["Oslo"]}]})
    s2 = script("s2", design={"parameters": [{"name": "city", "type": "list", "values": ["Rome"]}]})
    plan, root = build({"name": "D", "bandwidth_kbps": 512,
                        "groups": [{"name": "A", "script_id": "s1"}, {"name": "B", "script_id": "s2"}]},
                       scripts={"s1": s1, "s2": s2})
    assert plan.files == {"data/s1/city.csv": b"Oslo\n", "data/s2/city.csv": b"Rome\n"}
    assert sorted(props(e)["filename"] for e in root.iter("CSVDataSet")) == ["data/s1/city.csv", "data/s2/city.csv"]
    assert plan.properties == ["-Jhttpclient.socket.http.cps=64000", "-Jhttpclient.socket.https.cps=64000"]
    udv = [e.get("name") for e in root.find(".//TestPlan").iter("elementProp") if e.get("elementType") == "Argument"]
    assert udv == ["authorization"]
    assert any("authorization" in w for w in plan.warnings)


def test_percentage_mode_users_and_distributed_engines():
    cfg = {"name": "P", "distribution": "percent", "total_users": 40,
           "groups": [{"name": "A", "script_id": "s1", "percent": 75}, {"name": "B", "script_id": "s2", "percent": 25}]}
    plan, root = build(cfg, engines=2)
    assert [props(el)["ThreadGroup.num_threads"] for el, _ in thread_groups(root)] == ["15", "5"]   # per engine
    assert plan.total_users == 40
    with pytest.raises(ValidationError, match="add up to 100"):
        ScenarioConfig(**{**cfg, "groups": [{**cfg["groups"][0], "percent": 50}, cfg["groups"][1]]})


def test_imported_jmx_scripts_join_a_scenario():
    plan, root = build({"name": "J", "groups": [{"name": "A", "script_id": "j1", "users": 2}]},
                       scripts={"j1": script("j1", SHOP_JMX, "jmx")})
    [(tg, tree)] = thread_groups(root)
    assert props(tg)["ThreadGroup.num_threads"] == "2"
    assert [el.tag for el in tree if el.tag != "hashTree"] == ["ConfigTestElement", "HeaderManager",
                                                               "TransactionController", "TransactionController"]
    assert root.find(".//ResultCollector") is None


def test_missing_or_empty_scripts_are_reported():
    with pytest.raises(ScenarioError, match="no longer exists"):
        build({"name": "X", "groups": [{"name": "A", "script_id": "gone"}]})
    empty = script("s1")
    empty["overrides"] = {str(i): {"include": False} for i in range(len(empty["items"]))}
    with pytest.raises(ScenarioError, match="no kept requests"):
        build({"name": "X", "groups": [{"name": "A", "script_id": "s1"}]}, scripts={"s1": empty})


def sample(ts, label, elapsed=100, ok=True, thread="A 1-1", tx=False, users=2):
    return Sample(ts=ts, elapsed=elapsed, latency=elapsed, code="200" if ok else "500", message="tx" if tx else "OK",
                  success=ok, failure="", users=users, label=label, thread=thread, group_users=users, transaction=tx)


def test_scenario_summary_separates_requests_from_business_functions():
    samples = [sample(1000 + i * 100, "GET /a", 50 + i, thread="Buyers (1/2) 1-1") for i in range(10)]
    samples += [sample(1000 + i * 100, "01 Login", 200 + i * 10, ok=i != 9, thread="Buyers (2/2) 1-2", tx=True) for i in range(10)]
    samples += [sample(1500, "GET /b", 80, thread="Lookers 1-1", users=1)]
    requests, transactions = split(samples)
    assert len(requests) == 11 and len(transactions) == 10
    cfg = ScenarioConfig(name="s", groups=[{"name": "Buyers", "script_id": "x", "users": 2},
                                           {"name": "Lookers", "script_id": "y"}],
                         sla=[{"transaction": "Login", "metric": "p90", "limit": 250},
                              {"transaction": "*", "metric": "error_rate", "limit": 5},
                              {"transaction": "Checkout", "metric": "avg", "limit": 1}])
    s = scenario_summary(samples, cfg, ["Buyers", "Lookers"])
    login = s["transactions"][0]
    assert (login["display"], login["count"], login["failed"], login["p90"]) == ("Login", 10, 1, 280)
    assert s["requests"]["total_requests"] == 11
    assert [(r["metric"], r["status"]) for r in s["sla"]] == [("p90", "fail"), ("error_rate", "fail"), ("avg", "no_data")]
    assert s["verdict"] == "fail" and any("SLA failed: Login 90th percentile" in w["message"] for w in s["warnings"])
    assert {g["name"]: (g["requests"], g["peak_users"]) for g in s["groups"]} == {"Buyers": (10, 2), "Lookers": (1, 1)}
    assert group_of("Buyers (2/2) 1-7", ["Buyers", "Buy"]) == "Buyers"
    assert label_stats([], 1) == [] and evaluate_sla([], []) == []
