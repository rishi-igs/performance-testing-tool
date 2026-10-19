import json
import xml.etree.ElementTree as ET

from app.services.har_parser import parse_har
from tests.recordings import entry, har


def recorded_flow(value="test-flow-id-123456"):
    source = entry("https://shop.test/api/login", method="POST", start=0)
    source["response"]["content"]["text"] = json.dumps({"data": {"sessionId": value}})
    target = entry(
        "https://shop.test/api/account",
        method="POST",
        start=100,
        body=json.dumps({"sessionId": value}),
    )
    return har(source, target), value


def test_detects_and_sanitizes_reused_json_response_values(client):
    raw_har, value = recorded_flow()
    parsed = parse_har(raw_har)

    assert parsed["correlations"] == [{
        "source_index": 0,
        "target_indices": [1],
        "variable": "sessionId",
        "extractor": "json",
        "expression": "$.data.sessionId",
        "enabled": True,
    }]
    assert value not in json.dumps(parsed)
    assert parsed["items"][1]["body"] == '{"sessionId": "${sessionId}"}'
    assert "sessionId" in parsed["variables"]

    script_response = client.post("/scripts/import-har", content=raw_har)
    assert script_response.status_code == 201, script_response.text
    script = script_response.json()
    assert value not in json.dumps(script)
    plan = ET.fromstring(client.get(f"/scripts/{script['id']}/export-jmx").content)
    extractor = plan.find(".//JSONPostProcessor")
    assert extractor is not None
    assert extractor.findtext("stringProp[@name='JSONPostProcessor.referenceNames']") == "sessionId"
    assert extractor.findtext("stringProp[@name='JSONPostProcessor.jsonPathExprs']") == "$.data.sessionId"
    assert '{"sessionId": "${sessionId}"}' in ET.tostring(plan, encoding="unicode")

    changed = client.put(f"/scripts/{script['id']}/correlations", json={
        "correlations": [{**script["correlations"][0], "variable": "active_session", "enabled": False}],
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["correlations"][0]["variable"] == "active_session"
    disabled_export = client.get(f"/scripts/{script['id']}/export-jmx")
    assert disabled_export.status_code == 422 and "is disabled" in disabled_export.json()["detail"]
    enabled = client.put(f"/scripts/{script['id']}/correlations", json={
        "correlations": [{**changed.json()["correlations"][0], "enabled": True}],
    })
    assert enabled.status_code == 200
    renamed_plan = client.get(f"/scripts/{script['id']}/export-jmx").text
    assert "Extract active_session" in renamed_plan
    assert '{"sessionId": "${active_session}"}' in renamed_plan


def test_export_rejects_kept_correlated_request_when_source_is_excluded(client):
    raw_har, _ = recorded_flow("test-flow-id-654321")
    script = client.post("/scripts/import-har", content=raw_har).json()
    changed = client.put(
        f"/scripts/{script['id']}/filter",
        json={"changes": [{"index": 0, "include": False}]},
    )
    assert changed.status_code == 200
    result = client.get(f"/scripts/{script['id']}/export-jmx")
    assert result.status_code == 422
    assert "source request is excluded" in result.json()["detail"]


def test_correlated_authorization_header_uses_extracted_variable():
    value = "very-long-bearer-token-value"
    source = entry("https://shop.test/api/login", method="POST", start=0)
    source["response"]["content"]["text"] = json.dumps({"accessToken": value})
    target = entry("https://shop.test/api/account", start=100,
                   headers={"Authorization": "Bearer " + value})

    parsed = parse_har(har(source, target))
    assert parsed["items"][1]["headers"] == [["Authorization", "Bearer ${accessToken}"]]
    assert parsed["variables"] == ["accessToken"]
    assert value not in json.dumps(parsed)


def test_correlation_update_rejects_invalid_order_and_duplicate_variables(client):
    raw_har, _ = recorded_flow()
    script = client.post("/scripts/import-har", content=raw_har).json()
    rule = script["correlations"][0]
    invalid = client.put(
        f"/scripts/{script['id']}/correlations",
        json={"correlations": [{**rule, "source_index": 1}]},
    )
    assert invalid.status_code == 422

    duplicates = client.put(
        f"/scripts/{script['id']}/correlations",
        json={"correlations": [rule, {**rule, "source_index": 0, "expression": "$.other"}]},
    )
    assert duplicates.status_code == 422
