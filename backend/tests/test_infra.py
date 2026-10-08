"""Load generators and server monitors."""
import json
import socket
import threading
import xml.etree.ElementTree as ET

import pytest

from agent.perf_agent import make_server
from app.services.monitoring import monitor_warnings, parse_prometheus
from tests.conftest import wait_for
from tests.test_scenarios_api import imported, simple_har


@pytest.fixture
def listener():
    """A TCP port that accepts connections, standing in for jmeter-server's RMI registry."""
    sockets = []

    def make():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        sockets.append(s)
        return s.getsockname()[1]

    yield make
    for s in sockets:
        s.close()


@pytest.fixture
def agent():
    server = make_server("127.0.0.1", 0, token="agent-secret")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_generators_are_saved_and_checked(client, listener):
    port = listener()
    g = client.post("/generators", json={"name": "lg-1", "host": "127.0.0.1", "port": port}).json()
    assert g["config"]["port"] == port and g["status"] is None
    checked = client.post(f"/generators/{g['id']}/check").json()
    assert checked["status"]["ok"] and checked["status"]["connect_ms"] >= 0
    dead = client.post("/generators", json={"name": "lg-2", "host": "127.0.0.1", "port": free_port()}).json()
    status = client.post(f"/generators/{dead['id']}/check").json()["status"]
    assert not status["ok"] and "cannot connect" in status["error"]
    assert client.post("/generators", json={"name": "bad", "host": "not a host!"}).status_code == 422
    assert [x["config"]["name"] for x in client.get("/generators").json()] == ["lg-1", "lg-2"]
    assert client.delete(f"/generators/{dead['id']}").status_code == 204


def test_distributed_runs_split_users_across_generators(client, listener, target_url, settings):
    gens = [client.post("/generators", json={"name": f"lg-{i}", "host": "127.0.0.1", "port": listener()}).json()
            for i in range(2)]
    simple = imported(client, simple_har(target_url), "Simple")
    cfg = {"name": "Spread", "schedule": {"duration_seconds": 2}, "generators": [g["id"] for g in gens],
           "groups": [{"name": "B", "script_id": simple, "users": 6, "settings": {"think_time": {"mode": "ignore"}}}]}
    sid = client.post("/scenarios", json=cfg).json()["id"]
    rid = client.post(f"/scenarios/{sid}/run").json()["id"]
    wait_for(client, rid, timeout=60)
    argv = json.loads((settings.runs_dir / rid / "fake_argv.json").read_text())
    remote = argv[argv.index("-R") + 1]
    assert remote == ",".join(f"127.0.0.1:{g['config']['port']}" for g in gens)
    plan = ET.parse(settings.runs_dir / rid / "test_plan.jmx").getroot()
    assert next(e.text for e in plan.iter("stringProp") if e.get("name") == "ThreadGroup.num_threads") == "3"
    assert client.get(f"/tests/{rid}").json()["config"]["generators"][0]["name"] == "lg-0"

    client.put(f"/generators/{gens[1]['id']}", json={"name": "lg-1", "host": "127.0.0.1", "port": free_port()})
    r = client.post(f"/scenarios/{sid}/run")
    assert r.status_code == 422 and "Load generator lg-1" in r.json()["detail"]


def test_agent_monitor(client, agent):
    m = client.post("/monitors", json={"name": "app-01", "kind": "agent", "url": agent, "token": "agent-secret"}).json()
    assert m["config"]["token"] == "********"
    status = client.post(f"/monitors/{m['id']}/check").json()["status"]
    assert status["ok"], status
    values = {v["metric"]: v["value"] for v in status["values"]}
    assert 0 <= values["cpu_percent"] <= 100 and 0 < values["memory_percent"] < 100
    # Saving the masked token again keeps the real one
    client.put(f"/monitors/{m['id']}", json={**m["config"], "token": "********"})
    assert client.post(f"/monitors/{m['id']}/check").json()["status"]["ok"]
    wrong = client.post("/monitors", json={"name": "wrong", "kind": "agent", "url": agent, "token": "nope"}).json()
    status = client.post(f"/monitors/{wrong['id']}/check").json()["status"]
    assert not status["ok"] and "401" in status["error"]


def test_prometheus_and_local_monitors(client, target_url):
    m = client.post("/monitors", json={"name": "node", "kind": "prometheus", "url": f"{target_url}/prom-metrics",
                                       "preset": "node_exporter",
                                       "metrics": [{"id": "reqs", "label": "App requests", "metric": "app_requests_total"}]}).json()
    status = client.post(f"/monitors/{m['id']}/check").json()["status"]
    values = {v["metric"]: v["value"] for v in status["values"]}
    assert values == {"cpu_percent": pytest.approx(25, abs=0.5), "memory_percent": 75.0, "load1": 1.5, "reqs": 42.0}
    local = client.post("/monitors", json={"name": "console", "kind": "local"}).json()
    assert client.post(f"/monitors/{local['id']}/check").json()["status"]["ok"]
    assert client.post("/monitors", json={"name": "x", "kind": "prometheus", "url": f"{target_url}/prom-metrics"}).status_code == 422
    assert client.post("/monitors", json={"name": "x", "kind": "agent", "url": "http://169.254.169.254/"}).status_code == 422


def test_monitors_are_sampled_during_a_run(client, agent, target_url, settings):
    mon = client.post("/monitors", json={"name": "app-01", "kind": "agent", "url": agent, "token": "agent-secret",
                                         "interval_seconds": 2}).json()
    simple = imported(client, simple_har(target_url), "Simple")
    cfg = {"name": "Watched", "schedule": {"duration_seconds": 4}, "monitors": [mon["id"]],
           "groups": [{"name": "B", "script_id": simple, "settings": {"think_time": {"mode": "ignore"}}}]}
    sid = client.post("/scenarios", json=cfg).json()["id"]
    rid = client.post(f"/scenarios/{sid}/run").json()["id"]
    final = wait_for(client, rid, timeout=60)
    assert final["status"] == "completed"
    rows = (settings.runs_dir / rid / "monitors.csv").read_text().splitlines()
    assert rows[0] == "timestamp_ms,monitor,metric,label,unit,value" and len(rows) > 4
    series = {s["id"] for s in client.get(f"/tests/{rid}/analysis").json()["series"]}
    assert "mon:app-01:cpu_percent" in series and "mon:app-01:memory_percent" in series
    assert "Server monitors" in client.get(f"/reports/{rid}/summary.html").text
    assert client.get(f"/tests/{rid}").json()["summary"]["monitor_errors"] == {}


def test_parse_prometheus_and_monitor_findings():
    text = '# HELP x\nup 1\nreq_total{path="/a",note="say \\"hi\\""} 40\nreq_total{path="/b"} 2.5e+00\nbad line\n'
    assert parse_prometheus(text) == [("up", {}, 1.0), ("req_total", {"path": "/a", "note": 'say "hi"'}, 40.0),
                                      ("req_total", {"path": "/b"}, 2.5)]
    start = 1_000_000
    cpu = {"monitor": "db-01", "metric": "cpu_percent", "points": [(start + i * 5000, v) for i, v in enumerate([40, 88, 91, 95, 60])]}
    memory = {"monitor": "app-01", "metric": "memory_percent",
              "points": [(start + i * 5000, 40 + i * 3) for i in range(10)]}
    flat = {"monitor": "web", "metric": "memory_percent", "points": [(start + i * 5000, 50) for i in range(10)]}
    found = monitor_warnings([cpu, memory, flat], start)
    assert [w["code"] for w in found] == ["server_cpu_percent", "memory_growth"]
    assert "db-01 stayed above 85% from 5 s (peak 95%)" in found[0]["message"]
    assert "grew from about 43% to 64%" in found[1]["message"]
