"""API tests for importing, reviewing, grouping and exporting recorded scripts."""
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings
from tests.recordings import SHOP_HAR, SHOP_JMX


def import_har(client, name=None):
    r = client.post("/scripts/import-har", content=SHOP_HAR, params={"name": name} if name else None)
    assert r.status_code == 201, r.text
    return r.json()


def test_har_import_review_edit_and_export(client):
    script = import_har(client, "Checkout flow")
    assert script["name"] == "Checkout flow" and script["source"] == "har"
    assert script["summary"] == {"captured": 11, "kept": 5, "excluded": 6, "transactions": 3}
    assert [t["name"] for t in script["transactions"]] == ["Home", "Auth Login", "Products Search"]
    assert "headers" not in script["items"][0] and script["items"][5]["has_body"]
    for secret in ("s3cret-token", "s3cret-cookie", "demo-pass"):
        assert secret not in client.get(f"/scripts/{script['id']}").text
    assert [s["id"] for s in client.get("/scripts").json()] == [script["id"]]

    # Re-include a static file, rename one request's business function, then undo the include
    sid = script["id"]
    r = client.put(f"/scripts/{sid}/filter", json={"changes": [
        {"index": 2, "include": True}, {"index": 8, "transaction": "Product details"}]})
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["summary"]["kept"] == 6
    assert edited["items"][2]["include_source"] == "manual"
    assert [t["name"] for t in edited["transactions"]][-1] == "Product details"
    edited = client.put(f"/scripts/{sid}/filter", json={"changes": [{"index": 2, "include": None}]}).json()
    assert edited["summary"]["kept"] == 5 and edited["items"][2]["include_source"] == "auto"
    assert edited["items"][8]["transaction"] == "Product details"          # other edits are kept

    # New rules re-run the automatic choices without losing manual edits
    rules = {**edited["rules"], "transaction_rules": [{"name": "Login", "url_pattern": "*/auth/*"}]}
    edited = client.put(f"/scripts/{sid}/filter", json={"rules": rules}).json()
    assert [t["name"] for t in edited["transactions"]] == [
        "Home", "Login", "Products Search", "Product details"]

    r = client.get(f"/scripts/{sid}/export-jmx", params={"users": 5, "loops": 3})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert 'filename="Checkout-flow.jmx"' in r.headers["content-disposition"]
    root = ET.fromstring(r.content)
    assert [tc.get("testname") for tc in root.iter("TransactionController")] == [
        "01 Home", "02 Login", "03 Products Search", "04 Product details"]
    assert root.find(".//stringProp[@name='ThreadGroup.num_threads']").text == "5"


def test_edit_validation_and_export_limits(client):
    sid = import_har(client)["id"]
    assert client.put(f"/scripts/{sid}/filter", json={"changes": [{"index": 99, "include": True}]}).status_code == 422
    assert client.put(f"/scripts/{sid}/filter", json={"changes": [{"index": 0, "surprise": 1}]}).status_code == 422
    assert client.get(f"/scripts/{sid}/export-jmx", params={"users": 500}).status_code == 422
    everything_out = [{"index": i, "include": False} for i in range(11)]
    client.put(f"/scripts/{sid}/filter", json={"changes": everything_out})
    r = client.get(f"/scripts/{sid}/export-jmx")
    assert r.status_code == 422 and "every request is excluded" in r.json()["detail"]


def test_jmx_import_and_regrouped_export(client):
    r = client.post("/scripts/import-jmx", content=SHOP_JMX)
    assert r.status_code == 201, r.text
    script = r.json()
    assert script["name"] == "Shop flow" and script["source"] == "jmx"
    assert [t["name"] for t in script["transactions"]] == ["Home page", "Login"]
    root = ET.fromstring(client.get(f"/scripts/{script['id']}/export-jmx").content)
    assert [tc.get("testname") for tc in root.iter("TransactionController")] == ["01 Home page", "02 Login"]
    assert root.find(".//JSONPostProcessor") is not None


def test_csv_parameterization_replaces_recorded_value_and_exports_dataset(client):
    from tests.recordings import entry, har

    recorded = "rishi-test-user-4827"
    request = entry(
        "https://shop.test/api/login",
        method="POST",
        body=f'{{"username":"{recorded}","password":"sample-password"}}',
    )
    script = client.post("/scripts/import-har", content=har(request)).json()
    sid = script["id"]

    uploaded = client.put(
        f"/scripts/{sid}/parameters/csv",
        content=b"username,password\nuser-a,pass-a\nuser-b,pass-b\n",
        headers={"Content-Type": "text/csv"},
    )
    assert uploaded.status_code == 200
    assert uploaded.json() == {"csv_headers": ["username", "password"], "csv_row_count": 2}

    saved = client.put(f"/scripts/{sid}/parameters", json={
        "parameters": [{
            "variable": "username",
            "column": "username",
            "recorded_value": recorded,
        }],
        "recycle": False,
        "sharing_mode": "per_thread",
    })
    assert saved.status_code == 200, saved.text
    assert recorded not in saved.text
    detail = saved.json()
    assert detail["parameterization"] == {
        "parameters": [{"variable": "username", "column": "username", "replacements": 1}],
        "recycle": False,
        "sharing_mode": "per_thread",
        "csv_headers": ["username", "password"],
        "csv_row_count": 2,
    }
    assert "${username}" in client.app.state.db.get_script(sid)["items"][0]["body"]
    validation = client.post(f"/scripts/{sid}/validate")
    assert validation.status_code == 200
    assert validation.json()["status"] == "Pass"

    exported = client.get(f"/scripts/{sid}/export-jmx", params={"users": 5})
    assert exported.status_code == 200
    root = ET.fromstring(exported.content)
    dataset = root.find(".//CSVDataSet")
    assert dataset is not None
    assert dataset.findtext("stringProp[@name='filename']") == "${__P(perf.data.file,parameters.csv)}"
    assert dataset.findtext("stringProp[@name='variableNames']") == "username,password"
    assert dataset.findtext("stringProp[@name='shareMode']") == "shareMode.thread"
    assert dataset.findtext("boolProp[@name='recycle']") == "false"
    assert dataset.findtext("boolProp[@name='stopThread']") == "false"
    assert "${username}" in root.findtext(".//stringProp[@name='Argument.value']")
    assert recorded not in exported.text

    # The uploaded CSV content and the recorded sample value are not retained in the database.
    db = client.app.state.db
    stored_script = db.get_script(sid)
    assert recorded not in str(stored_script)
    assert "user-a" not in str(stored_script)

    renamed = client.put(f"/scripts/{sid}/parameters", json={
        "parameters": [{"variable": "login_name", "column": "username"}],
    })
    assert renamed.status_code == 200, renamed.text
    assert "${login_name}" in client.app.state.db.get_script(sid)["items"][0]["body"]
    assert "${username}" not in client.app.state.db.get_script(sid)["items"][0]["body"]


def test_parameter_csv_and_mapping_validation(client):
    from tests.recordings import entry, har

    request = entry("https://shop.test/api/login", method="POST", body='{"username":"recorded-user"}')
    sid = client.post("/scripts/import-har", content=har(request)).json()["id"]
    assert client.put(f"/scripts/{sid}/parameters", json={
        "parameters": [{"variable": "username", "column": "username", "recorded_value": "recorded-user"}],
    }).status_code == 422
    assert client.put(
        f"/scripts/{sid}/parameters/csv",
        content=b"username,username\none,two\n",
    ).status_code == 422
    assert client.put(
        f"/scripts/{sid}/parameters/csv",
        content=b"username,email\none\n",
    ).status_code == 422

    client.put(f"/scripts/{sid}/parameters/csv", content=b"email\none@example.test\n")
    missing = client.put(f"/scripts/{sid}/parameters", json={
        "parameters": [{"variable": "username", "column": "email", "recorded_value": "not-in-request"}],
    })
    assert missing.status_code == 422 and "not found" in missing.json()["detail"]


def test_har_validator_compares_kept_requests_with_generated_plan(client):
    from tests.recordings import entry, har

    raw = har(
        entry("https://shop.test/api/start?mode=basic", headers={"Accept": "application/json"}),
        entry("https://shop.test/api/finish", method="POST", status=201,
              headers={"Content-Type": "application/json"}, body='{"complete":true}'),
    )
    script = client.post("/scripts/import-har", content=raw).json()
    result = client.post(f"/scripts/{script['id']}/validate")

    assert result.status_code == 200, result.text
    report = result.json()
    assert report["status"] == "Pass"
    assert report["summary"] == {
        "requests_checked": 2,
        "requests_passed": 2,
        "requests_failed": 0,
        "unresolved_dynamic_values": 0,
    }
    assert [request["method"] for request in report["requests"]] == ["GET", "POST"]
    assert report["requests"][0]["recorded_endpoint"] == report["requests"][0]["generated_endpoint"]
    assert all(check["status"] == "pass" for request in report["requests"] for check in request["checks"])
    for request in report["requests"]:
        assert {check["field"] for check in request["checks"]} == {
            "endpoint", "method", "headers", "body", "response_code", "tokens", "order",
        }
        assert all(check["status"] == "pass" for check in request["checks"])


def test_har_validator_reports_likely_dynamic_literals_without_exposing_values(client):
    from tests.recordings import entry, har

    raw = har(entry(
        "https://shop.test/api/items/123456?session=private-session-value",
        method="POST",
        body='{"session_id":"another-private-session"}',
    ))
    script = client.post("/scripts/import-har", content=raw).json()
    result = client.post(f"/scripts/{script['id']}/validate")

    assert result.status_code == 200
    report = result.json()
    assert report["status"] == "Pass with warnings"
    assert report["summary"]["unresolved_dynamic_values"] >= 3
    assert "123456" not in result.text
    assert "private-session-value" not in result.text
    assert "another-private-session" not in result.text


def test_har_validator_flags_disabled_correlation_used_by_kept_request(client):
    import json

    from tests.recordings import entry, har

    token = "private-session-token-123456"
    source = entry("https://shop.test/api/login")
    source["response"]["content"]["text"] = json.dumps({"sessionToken": token})
    raw = har(source, entry(f"https://shop.test/api/account?session={token}"))
    script = client.post("/scripts/import-har", content=raw).json()
    rule = script["correlations"][0]
    disabled = client.put(f"/scripts/{script['id']}/correlations", json={
        "correlations": [{**rule, "enabled": False}],
    })
    assert disabled.status_code == 200

    report = client.post(f"/scripts/{script['id']}/validate").json()
    assert report["status"] == "Fail"
    assert "disabled_correlation" in {issue["code"] for issue in report["issues"]}
    assert "unknown_variable" in {issue["code"] for issue in report["issues"]}
    assert token not in str(report)


def test_assertion_builder_suggests_status_and_exports_multiple_assertion_types(client):
    from tests.recordings import entry, har

    sid = client.post("/scripts/import-har", content=har(entry(
        "https://shop.test/api/check", status=200,
    ))).json()["id"]
    detail = client.get(f"/scripts/{sid}").json()
    assert detail["assertions"] == [{
        "scope": "request", "request_index": 0,
        "checks": [{"type": "status_code", "expected": 200}],
    }]
    assert detail["assertion_warnings"] == []

    updated = client.put(f"/scripts/{sid}/assertions", json={"assertions": [
        {"scope": "global", "checks": [
            {"type": "text_contains", "value": "ready"},
            {"type": "json_path", "path": "$.status", "expected": "ready"},
            {"type": "duration", "max_ms": 800},
            {"type": "response_size", "min_bytes": 2, "max_bytes": 2048},
            {"type": "header_value", "name": "content-type", "value": "application/json"},
        ]},
        {"scope": "transaction", "transaction": detail["transactions"][0]["name"], "checks": [
            {"type": "status_code", "expected": "2xx"},
        ]},
        {"scope": "request", "request_index": 0, "checks": [
            {"type": "text_not_contains", "value": "fatal"},
            {"type": "text_equals", "value": "ready"},
            {"type": "text_matches", "value": "ready"},
        ]},
    ]})
    assert updated.status_code == 200, updated.text
    assert updated.json()["assertion_warnings"] == []

    root = ET.fromstring(client.get(f"/scripts/{sid}/export-jmx").content)
    assert root.find(".//JSONPathAssertion") is not None
    assert root.find(".//DurationAssertion") is not None
    assert len(root.findall(".//SizeAssertion")) == 2
    fields = [node.text for node in root.findall(".//ResponseAssertion/stringProp[@name='Assertion.test_field']")]
    assert "Assertion.response_data" in fields and "Assertion.response_headers" in fields


def test_assertion_validation_and_no_assertion_warning(client):
    from tests.recordings import entry, har

    sid = client.post("/scripts/import-har", content=har(entry("https://shop.test/api/check"))).json()["id"]
    invalid = client.put(f"/scripts/{sid}/assertions", json={"assertions": [
        {"scope": "request", "request_index": 20,
         "checks": [{"type": "status_code", "expected": 200}]},
    ]})
    assert invalid.status_code == 422
    cleared = client.put(f"/scripts/{sid}/assertions", json={"assertions": []})
    assert cleared.status_code == 200
    assert cleared.json()["assertion_warnings"] == ["Request 1 has no assertion."]


def test_debug_run_stores_samples_supports_search_and_diagnosis(client, target_url):
    from tests.recordings import entry, har

    raw = har(entry(f"{target_url}/status/500", status=200, start=0))
    sid = client.post("/scripts/import-har", content=raw).json()["id"]
    response = client.post(f"/debug-runs/scripts/{sid}?users=1&loops=1")
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]

    import time
    for _ in range(100):
        run = client.get(f"/debug-runs/{run_id}").json()
        if run["status"] != "running":
            break
        time.sleep(0.05)
    assert run["status"] == "completed", run
    assert run["summary"] == {"samples": 1, "passed": 0, "failed": 1, "assertion_failures": 1}

    all_samples = client.get(f"/debug-runs/{run_id}/samples").json()["samples"]
    assert [sample["success"] for sample in all_samples] == [False]
    assert all(sample["timing"]["total_ms"] >= 0 for sample in all_samples)
    assert client.get(f"/debug-runs/{run_id}/samples", params={"failed_only": True}).json()["samples"] == all_samples
    assert client.get(f"/debug-runs/{run_id}/samples", params={"search": "status 500"}).json()["samples"] == all_samples
    diagnosis = client.get(f"/debug-runs/{run_id}/diagnosis").json()
    assert diagnosis["first_failing_request"] == 0
    assert client.get(f"/debug-runs/{run_id}/samples/99").status_code == 404
    assert client.get(f"/debug-runs/{run_id}/results.jtl").status_code == 200
    assert client.get(f"/debug-runs/{run_id}/jmeter.log").status_code == 200
    assert client.get(f"/debug-runs/scripts/{sid}").json()[0]["id"] == run_id

    rerun = client.post(f"/debug-runs/{run_id}/rerun")
    assert rerun.status_code == 202, rerun.text
    rerun_id = rerun.json()["id"]
    for _ in range(100):
        rerun_status = client.get(f"/debug-runs/{rerun_id}").json()
        if rerun_status["status"] != "running":
            break
        time.sleep(0.05)
    assert rerun_status["status"] == "completed", rerun_status
    assert [run["id"] for run in client.get(f"/debug-runs/scripts/{sid}").json()] == [rerun_id, run_id]


def test_debug_sample_masks_sensitive_headers_and_json_values(client, target_url):
    from tests.recordings import entry, har

    raw = har(entry(
        f"{target_url}/echo",
        method="POST",
        headers={"Authorization": "Bearer demo-secret", "Content-Type": "application/json"},
        body='{"password":"demo-secret","ok":true}',
    ))
    sid = client.post("/scripts/import-har", content=raw).json()["id"]
    started = client.post(f"/debug-runs/scripts/{sid}?users=1&loops=1")
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]
    import time
    for _ in range(100):
        run = client.get(f"/debug-runs/{run_id}").json()
        if run["status"] != "running":
            break
        time.sleep(0.05)
    assert run["status"] == "completed", run

    sample = client.get(f"/debug-runs/{run_id}/samples/0").json()
    assert "Authorization: ********" in sample["request"]["headers"]
    assert "demo-secret" not in sample["request"]["body"]
    assert "demo-secret" not in sample["response"]["body"]
    assert "********" in sample["request"]["body"]
    assert "********" in sample["response"]["body"]

def test_har_validator_warns_that_recorded_credential_variables_need_values(client):
    from tests.recordings import entry, har

    script = client.post("/scripts/import-har", content=har(entry(
        "https://shop.test/api/profile",
        headers={"Authorization": "Bearer recorded-private-credential"},
    ))).json()
    result = client.post(f"/scripts/{script['id']}/validate")

    assert result.status_code == 200
    report = result.json()
    assert report["status"] == "Pass with warnings"
    assert any(issue["code"] == "manual_variable" for issue in report["issues"])
    assert "recorded-private-credential" not in result.text


def test_har_validator_rejects_jmx_import(client):
    from tests.recordings import SHOP_JMX

    script = client.post("/scripts/import-jmx", content=SHOP_JMX).json()
    result = client.post(f"/scripts/{script['id']}/validate")
    assert result.status_code == 422


def test_bad_uploads_are_rejected(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, max_upload_bytes=1024))) as c:
        assert c.post("/scripts/import-har", content=b"").status_code == 422
        r = c.post("/scripts/import-har", content=b"{not json")
        assert r.status_code == 422 and "JSON" in r.json()["detail"]
        assert c.post("/scripts/import-jmx", content=b"<x/>").status_code == 422
        assert c.post("/scripts/import-har", content=b"x" * 2048).status_code == 413
        assert c.get("/scripts") .json() == []


def test_scripts_need_the_api_key_and_can_be_deleted(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, api_key="k3y"))) as c:
        assert c.post("/scripts/import-har", content=SHOP_HAR).status_code == 401
        assert c.get("/scripts").status_code == 401
        key = {"X-API-Key": "k3y"}
        sid = c.post("/scripts/import-har", content=SHOP_HAR, headers=key).json()["id"]
        assert c.get(f"/scripts/{sid}/export-jmx").status_code == 401
        assert c.post(f"/scripts/{sid}/validate").status_code == 401
        assert c.delete(f"/scripts/{sid}", headers=key).status_code == 204
        assert c.get(f"/scripts/{sid}", headers=key).status_code == 404
        assert c.delete(f"/scripts/{sid}", headers=key).status_code == 404
