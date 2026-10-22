"""HAR comparison validator (requirements 8.5): the plan against the recording, before every run."""
import json
import time

from app.services.har_validator import _json_with_variables, is_secret_name, mask_body
from tests.recorder import Recorder, record_correlation_flow
from tests.recordings import SHOP_HAR, entry, har


def finished_replay(client, script_id, **params):
    run = client.post(f"/scripts/{script_id}/debug-run", params=params).json()
    deadline = time.time() + 40
    while time.time() < deadline:
        detail = client.get(f"/debug-runs/{run['id']}").json()
        if detail["status"] != "running":
            return detail
        time.sleep(0.2)
    raise AssertionError("the debug replay did not finish")


def imported(client, raw, name="flow"):
    r = client.post("/scripts/import-har", content=raw, params={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def test_the_sample_flow_passes_with_every_dynamic_value_correlated(client, target_url):
    """Definition of done: no unresolved dynamic values for the sample application flow."""
    script = imported(client, record_correlation_flow(target_url))
    report = client.post(f"/scripts/{script['id']}/validate").json()
    assert report["status"] == "pass", [d["message"] for r in report["requests"] for d in r["differences"] if d["severity"] != "info"]
    assert report["label"] == "Pass" and report["unresolved_values"] == [] and report["unresolved_variables"] == []
    assert report["counts"]["requests"] == 10 and report["counts"]["pass"] == 10
    by_label = {r["label"]: r for r in report["requests"]}
    private = by_label["GET /private"]
    assert private["recorded"]["method"] == "GET" and private["generated"]["url"].endswith("/private")
    auth = next(h for h in private["generated"]["headers"] if h[0] == "Authorization")
    assert auth[1] == "Bearer ${token}"                     # a variable is not a secret: shown as is
    assert client.get(f"/scripts/{script['id']}").json()["validation"]["comparison"]["status"] == "pass"


def test_a_value_no_response_issued_is_unresolved_until_marked_static(client, target_url):
    rec = Recorder(target_url)
    rec.request("POST", "/echo", body='{"sku": "AB12CD34EF", "qty": 2}', content_type="application/json")
    script = imported(client, rec.har())
    report = client.get(f"/scripts/{script['id']}/validation").json()
    assert report["status"] == "warn" and report["label"] == "Pass with warnings"
    [value] = report["unresolved_values"]
    assert value["name"] == "sku" and value["value"] == "AB12CD34EF" and value["preview"].startswith("AB12")
    token_diff = next(d for d in report["requests"][0]["differences"] if d["part"] == "token")
    assert token_diff["type"] == "unresolved" and "mark it static" in token_diff["message"]
    client.put(f"/scripts/{script['id']}/design", json={"static_values": ["AB12CD34EF"]})
    assert client.get(f"/scripts/{script['id']}/validation").json()["status"] == "pass"
    # Replacing it with a parameter makes it a variable on the generated side
    client.put(f"/scripts/{script['id']}/design", json={
        "static_values": [], "parameters": [{"name": "sku", "type": "list", "values": ["AB12CD34EF", "ZZ99YY88XX"]}],
        "replacements": [{"find": "AB12CD34EF", "variable": "sku"}]})
    report = client.get(f"/scripts/{script['id']}/validation").json()
    assert report["status"] == "pass"
    field = next(d for d in report["requests"][0]["differences"] if d["part"] == "JSON field")
    assert (field["type"], field["name"], field["recorded"], field["generated"]) == ("variable", "$.sku", "AB12CD34EF", "${sku}")


def test_an_unset_variable_fails_validation_and_blocks_a_scenario_run(client, target_url):
    rec = Recorder(target_url)
    rec.request("GET", "/fixed")
    script = imported(client, rec.har())
    client.put(f"/scripts/{script['id']}/design", json={"replacements": [{"find": "fixed", "variable": "nowhere"}]})
    report = client.get(f"/scripts/{script['id']}/validation").json()
    assert report["status"] == "fail" and report["unresolved_variables"] == [{"name": "nowhere", "requests": [0]}]
    diff = next(d for d in report["requests"][0]["differences"] if d["part"] == "variable")
    assert diff["severity"] == "fail" and "nothing sets it" in diff["message"]
    scenario = client.post("/scenarios", json={"name": "S", "schedule": {"duration_seconds": 2},
                                               "groups": [{"name": "A", "script_id": script["id"]}]}).json()
    r = client.post(f"/scenarios/{scenario['id']}/run")
    assert r.status_code == 422 and "fails validation" in r.json()["detail"] and "${nowhere}" in r.json()["detail"]
    preview = client.get(f"/scenarios/{scenario['id']}/preview").json()
    assert any("fails validation" in w for w in preview["warnings"])
    # A debug replay still runs (that is how problems are found) and keeps the validation result
    detail = finished_replay(client, script["id"])
    assert detail["summary"]["validation"]["status"] == "fail"


def test_moving_a_request_before_the_one_its_value_comes_from_fails(client, target_url):
    script = imported(client, record_correlation_flow(target_url))
    sid, count = script["id"], len(script["items"])
    login = next(i["index"] for i in script["items"] if i["label"] == "POST /login")
    private = next(i["index"] for i in script["items"] if i["label"] == "GET /private")
    order = list(range(count))
    order.remove(private)
    order.insert(order.index(login), private)            # /private now runs before /login issues the token
    moved = client.put(f"/scripts/{sid}/filter", json={"order": order}).json()
    assert moved["order"] == order and moved["reordered"]
    report = client.get(f"/scripts/{sid}/validation").json()
    assert report["status"] == "fail"
    row = next(r for r in report["requests"] if r["index"] == private)
    assert any(d["part"] == "order" and d["severity"] == "fail" and "runs after this request" in d["message"]
               for d in row["differences"])
    assert any(d["part"] == "order" and d["severity"] == "warning" for d in row["differences"])
    assert client.put(f"/scripts/{sid}/filter", json={"order": [0, 0, 1]}).status_code == 422
    back = client.put(f"/scripts/{sid}/filter", json={"order": []}).json()
    assert back["order"] == list(range(count)) and not back["reordered"]
    assert client.get(f"/scripts/{sid}/validation").json()["status"] == "pass"


def test_a_session_cookie_from_before_the_recording_is_flagged(client):
    raw = har(entry("https://shop.test/api/cart", headers={"Cookie": "sessionid=abc; _ga=GA1.2"}),
              entry("https://shop.test/api/me", headers={"Cookie": "sessionid=abc"}))
    script = imported(client, raw)
    assert script["items"][0]["kind"] == "http"
    report = client.get(f"/scripts/{script['id']}/validation").json()
    cookies = [d for d in report["requests"][0]["differences"] if d["part"] == "cookie"]
    assert {(d["name"], d["severity"]) for d in cookies} == {("sessionid", "warning"), ("_ga", "info")}
    assert "before the recording started" in cookies[0]["message"]


def test_secrets_are_masked_in_the_report_unless_revealed(client, target_url):
    rec = Recorder(target_url)
    rec.request("POST", "/echo", body="user=demo&password=hunter2hunter2&next=%2Fhome",
                content_type="application/x-www-form-urlencoded")
    script = imported(client, rec.har())
    text = json.dumps(client.get(f"/scripts/{script['id']}/validation").json())
    assert "hunter2" not in text and "********" in text
    assert "hunter2" in json.dumps(client.get(f"/scripts/{script['id']}/validation", params={"reveal": True}).json())


def test_shop_recording_reports_its_credential_placeholder():
    from app.models.script import FilterRules
    from app.services import har_validator, request_filter, script_builder
    from app.services.har_parser import parse_har
    parsed = parse_har(SHOP_HAR)
    script = {"id": "s", "name": "shop", "source": "har", "items": parsed["items"], "variables": parsed["variables"],
              "rules": FilterRules().model_dump(), "overrides": {}, "order": [], "design": {}}
    view = request_filter.evaluate("har", script["items"], FilterRules(), {}, [])
    tree, built = script_builder.build_tree(script, view)
    report = har_validator.validate(script, view, tree, built)
    assert report["status"] == "warn"
    assert any(d["part"] == "variable" and d["name"] == "authorization" and d["severity"] == "warning"
               for r in report["requests"] for d in r["differences"])


def test_json_bodies_with_variables_and_masking_helpers():
    assert _json_with_variables('{"qty": ${qty}, "who": "Bearer ${token}", "n": ${__Random(1,9,)}}') == {
        "qty": "${qty}", "who": "Bearer ${token}", "n": "${__Random(1,9,)}"}
    assert mask_body('{"user": "a", "password": "p4ss", "nested": {"api_key": 123}}') == \
        '{"user": "a", "password": "********", "nested": {"api_key": "********"}}'
    assert mask_body('{"password": "${password}"}') == '{"password": "${password}"}'
    assert mask_body("a=1&pwd=x&shipping=road") == "a=1&pwd=********&shipping=road"
    assert is_secret_name("accessToken") and is_secret_name("user_pin") and not is_secret_name("shipping")
    assert not is_secret_name("opinion") and is_secret_name("X-API-Key".replace("-", "_"))


def test_validation_runs_before_a_debug_replay(client, target_url):
    script = imported(client, record_correlation_flow(target_url))
    detail = finished_replay(client, script["id"])
    assert detail["summary"]["validation"]["status"] == "pass" and detail["summary"]["validation"]["label"] == "Pass"
    assert client.get(f"/scripts/{script['id']}/debug-runs").json()[0]["validation"] == "pass"
