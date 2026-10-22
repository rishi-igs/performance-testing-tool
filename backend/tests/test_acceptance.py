"""Phase 3 exit criterion, automated: an authenticated multi-step flow, recorded, passes a debug run
with every check green and no unresolved dynamic values.

The flow is the /demo page of the sample app (sign in, then place an order), made the way the
page's script makes it in a browser. The same page is what to record in a real browser for the
manual acceptance check (README, "Acceptance check in a real browser").
"""
import time

from tests.recorder import record_demo_flow
from tests.test_debug_api import reset


def test_a_recorded_sign_in_and_order_flow_replays_cleanly(client, target_url):
    script = client.post("/scripts/import-har", content=record_demo_flow(target_url), params={"name": "Demo shop"}).json()
    sid = script["id"]
    design = script["design"]
    assert {c["variable"] for c in design["correlations"]} >= {"token", "csrf", "XSRF_TOKEN"}
    assert design["data_files"][0]["columns"] == ["username", "password"]      # the recorded sign-in became test data
    report = client.get(f"/scripts/{sid}/validation").json()
    assert report["status"] == "pass" and report["unresolved_values"] == [] and report["unresolved_variables"] == []

    reset(target_url)                     # nothing the recording received is valid any more
    run = client.post(f"/scripts/{sid}/debug-run").json()
    deadline = time.time() + 60
    while time.time() < deadline:
        detail = client.get(f"/debug-runs/{run['id']}").json()
        if detail["status"] != "running":
            break
        time.sleep(0.2)
    s = detail["summary"]
    assert s["verdict"] == "pass", [(i["label"], i["issues"]) for i in s["items"] if i["issues"]]
    assert s["passed"] == s["requests"] == 8 and s["validation"]["status"] == "pass"
    assert all(c["failed"] is False for i in s["items"] for c in i["checks"])
    assert all(not v["not_found"] for v in s["variables"].values())
