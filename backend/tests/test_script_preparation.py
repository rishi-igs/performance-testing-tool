"""Script preparation (requirements 8.3-8.8): ordering, data files, checks per business function,
debug replays with several users, search, masking, logs."""
import io
import time
import xml.etree.ElementTree as ET
import zipfile

from tests.recorder import Recorder, record_correlation_flow
from tests.recordings import SHOP_HAR, entry, har


def imported(client, raw, name="flow"):
    r = client.post("/scripts/import-har", content=raw, params={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def replay(client, script_id, **params):
    r = client.post(f"/scripts/{script_id}/debug-run", params=params)
    assert r.status_code == 201, r.text
    deadline = time.time() + 60
    while time.time() < deadline:
        run = client.get(f"/debug-runs/{r.json()['id']}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.2)
    raise AssertionError("the debug replay did not finish")


def test_a_replay_with_several_users_and_iterations_keeps_every_result(client, target_url):
    script = imported(client, record_correlation_flow(target_url))
    run = replay(client, script["id"], users=2, iterations=2)
    s = run["summary"]
    assert s["verdict"] == "pass", [i["issues"] for i in s["items"]]
    assert (s["users"], s["iterations"]) == (2, 2) and all(i["runs"] == 4 and i["failed_runs"] == 0 for i in s["items"])
    login = next(i for i in s["items"] if i["label"] == "POST /login")
    last = client.get(f"/debug-runs/{run['id']}/samples/{login['index']}", params={"occurrence": 3}).json()
    assert last["occurrence"] == 3 and last["occurrences"] == 4
    assert client.get(f"/debug-runs/{run['id']}/samples/{login['index']}", params={"occurrence": 4}).status_code == 404
    assert client.post(f"/scripts/{script['id']}/debug-run", params={"users": 6}).status_code == 422


def test_replays_mask_tokens_search_bodies_and_offer_the_logs(client, target_url):
    script = imported(client, record_correlation_flow(target_url))
    run = replay(client, script["id"])
    login = next(i for i in run["summary"]["items"] if i["label"] == "POST /login")
    private = next(i for i in run["summary"]["items"] if i["label"] == "GET /private")
    assert private["uses"] == ["token"]                    # the variables each request sends
    captured = run["summary"]["variables"]
    assert captured["token"]["preview"] == "********" and captured["csrf"]["preview"].startswith("ab12")
    masked = client.get(f"/debug-runs/{run['id']}/samples/{login['index']}").json()
    assert '"token": "********"' in masked["response_body"] and not masked["revealed"]
    shown = client.get(f"/debug-runs/{run['id']}/samples/{login['index']}", params={"reveal": True}).json()
    assert '"token":"ab12' in shown["response_body"]
    hits = client.get(f"/debug-runs/{run['id']}/search", params={"q": "order form"}).json()["hits"]
    assert hits == [{"index": 0, "occurrence": 0, "where": ["response body"]}]
    assert client.get(f"/debug-runs/{run['id']}/search", params={"q": shown["response_body"][12:30]}).json()["hits"] == []
    log = client.get(f"/debug-runs/{run['id']}/jmeter.log")
    assert log.status_code == 200 and "JMeter" in log.text
    raw = client.get(f"/debug-runs/{run['id']}/results.xml")
    assert raw.status_code == 200 and raw.text.lstrip().startswith("<?xml")


def test_requests_can_be_moved_and_polling_left_out(client):
    poll = [entry("https://shop.test/api/notifications", start=100 * i) for i in range(4)]
    raw = har(entry("https://shop.test/api/login", method="POST", start=0), *[{**p} for p in poll],
              entry("https://shop.test/api/cart", start=900))
    script = imported(client, raw)
    sid = script["id"]
    assert script["summary"]["kept"] == 6
    rules = {**script["rules"], "exclude_repeated_polling": True}
    s = client.put(f"/scripts/{sid}/filter", json={"rules": rules}).json()
    reasons = [i["auto_reason"] for i in s["items"]]
    assert s["summary"]["kept"] == 3 and reasons[1] is None and reasons[2].startswith("Repeated polling")
    s = client.put(f"/scripts/{sid}/filter", json={"order": [5, 0, 1, 2, 3, 4]}).json()
    assert s["order"] == [5, 0, 1, 2, 3, 4] and s["reordered"]
    plan = client.get(f"/scripts/{sid}/export-jmx").text
    assert plan.index("/api/cart") < plan.index("/api/login")


def _controller_tree(root, name):
    for parent in root.iter("hashTree"):
        kids = list(parent)
        for i, el in enumerate(kids):
            if el.tag == "TransactionController" and el.get("testname", "").endswith(name):
                return kids[i + 1]
    raise AssertionError(f"no Transaction Controller {name}")


def test_checks_can_cover_a_business_function_and_unchecked_requests_are_listed(client):
    script = imported(client, SHOP_HAR)
    sid = script["id"]
    login = next(t for t in script["transactions"] if "Login" in t["name"])
    s = client.put(f"/scripts/{sid}/design", json={"checks": [{"transaction": login["name"], "type": "text", "value": "ok"}]}).json()
    assert s["design"]["checks"][0]["transaction"] == login["name"] and not s["validation"]["warnings"]
    root = ET.fromstring(client.get(f"/scripts/{sid}/export-jmx").content)
    named = lambda tree: sum(1 for a in tree.iter("ResponseAssertion") if a.get("testname") == "Check: text ok")  # noqa: E731
    assert named(root) == named(_controller_tree(root, login["name"])) == login["requests"]
    assert client.put(f"/scripts/{sid}/design", json={"checks": [{"item": 0, "transaction": login["name"],
                                                                   "type": "text", "value": "x"}]}).status_code == 422
    s = client.put(f"/scripts/{sid}/design", json={"checks": [], "settings": {"status_checks": False}}).json()
    kept = [i["index"] for i in s["items"] if i["include"] and i["kind"] == "http"]
    assert s["validation"]["unchecked"] == kept and s["validation"]["status_only_checks"] == []


def test_where_each_variable_is_used(client, target_url):
    script = imported(client, record_correlation_flow(target_url))
    where = script["validation"]["where"]
    private = next(i["index"] for i in script["items"] if i["label"] == "GET /private")
    assert where["token"] == [{"index": private, "where": ["header Authorization"]}]
    submit = next(i["index"] for i in script["items"] if i["label"] == "POST /page/submit")
    assert {"index": submit, "where": ["body"]} in where["csrf"]


def test_data_files_with_other_delimiters_and_too_few_rows(client, target_url):
    rec = Recorder(target_url)
    rec.request("GET", "/fixed?city=Oslo")
    script = imported(client, rec.har())
    sid = script["id"]
    s = client.post(f"/scripts/{sid}/files", params={"name": "cities.csv", "delimiter": ";"},
                    content=b"city;country\nParis;FR\nRome;IT\n").json()
    f = s["design"]["data_files"][0]
    assert f["columns"] == ["city", "country"] and f["rows"] == 2 and f["delimiter"] == ";"
    s = client.put(f"/scripts/{sid}/design", json={"replacements": [{"find": "Oslo", "variable": "city"}],
                                                   "data_files": [{**f, "recycle": False, "delimiter": ","}]}).json()
    assert s["design"]["data_files"][0]["delimiter"] == ";"         # it comes from the uploaded file
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/scripts/{sid}/export-jmx").content))
    plan = z.read(next(n for n in z.namelist() if n.endswith(".jmx"))).decode()
    assert '<stringProp name="delimiter">;</stringProp>' in plan
    run = replay(client, sid, users=3)
    assert run["summary"]["verdict"] == "pass"
    assert any("cities.csv has 2 rows for 3 users" in w for w in run["summary"]["warnings"])
    scenario = client.post("/scenarios", json={"name": "S", "schedule": {"duration_seconds": 2},
                                               "groups": [{"name": "A", "script_id": sid, "users": 5}]}).json()
    warnings = client.get(f"/scenarios/{scenario['id']}/preview").json()["warnings"]
    assert any("A: cities.csv has 2 rows for 5 users" in w for w in warnings)
