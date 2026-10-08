import json

from app.services.correlation import _boundaries, looks_dynamic, replace_value
from app.services.har_parser import parse_har
from tests.recorder import Recorder, record_correlation_flow
from tests.recordings import entry, har


def test_a_recorded_flow_is_correlated_automatically(target_url):
    raw = record_correlation_flow(target_url)
    recorded = json.loads(raw)["log"]["entries"]
    parsed = parse_har(raw)
    rules = {c["variable"]: c for c in parsed["design"]["correlations"]}
    assert set(rules) == {"csrf", "token", "id", "code", "XSRF_TOKEN"}

    assert rules["csrf"]["extractor"] == "boundary" and rules["csrf"]["source"] == 0 and rules["csrf"]["right"]
    assert (rules["token"]["extractor"], rules["token"]["expression"], rules["token"]["source"]) == ("json", "$.token", 2)
    assert (rules["id"]["extractor"], rules["id"]["expression"]) == ("json", "$.order.id")
    assert (rules["code"]["extractor"], rules["code"]["scope"], rules["code"]["source"]) == ("regex", "headers", 6)
    assert rules["XSRF_TOKEN"]["scope"] == "headers" and rules["XSRF_TOKEN"]["used_in"] == [9]

    items = parsed["items"]
    assert items[1]["body"] == "csrf=${csrf}&item=book"
    assert ["Authorization", "Bearer ${token}"] in items[3]["headers"]
    assert items[5]["url"].endswith("/api/orders/${id}")
    assert "code=${code}&state=x1" in items[7]["url"]
    assert ["X-XSRF-TOKEN", "${XSRF_TOKEN}"] in items[9]["headers"]
    # No recorded dynamic value is stored, and no credential placeholder is needed
    stored = json.dumps(items)
    login_token = json.loads(recorded[2]["response"]["content"]["text"])["token"]
    assert login_token not in stored and parsed["variables"] == []
    assert any("5 dynamic value" in w for w in parsed["warnings"])


def test_values_the_browser_generated_become_parameter_suggestions():
    request_id = "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b"
    data = har(entry("https://shop.test/api/cart", method="POST", body=json.dumps({"requestId": request_id}),
                     headers={"Content-Type": "application/json"}))
    parsed = parse_har(data)
    assert parsed["design"]["correlations"] == []
    [suggestion] = parsed["suggestions"]["parameters"]
    assert suggestion["name"] == "requestId" and suggestion["kind"] == "uuid"
    assert suggestion["value"] == request_id            # not a secret: kept so the UI can parameterize it


def test_check_suggestions_come_from_stable_response_fields(target_url):
    rec = Recorder(target_url)
    rec.request("GET", "/page/form")
    rec.request("POST", "/login", body="{}", content_type="application/json")
    suggestions = parse_har(rec.har())["suggestions"]["checks"]
    assert {"item": 0, "type": "text", "value": "Order form"} in suggestions
    assert any(s["item"] == 1 and s["type"] == "json_path" for s in suggestions)


def test_replacement_keeps_whole_values_and_handles_url_encoding():
    text, n = replace_value("a=ab12cd34&b=ab12cd345&c=x%2Fab12cd34", "ab12cd34", "v")
    assert text == "a=${v}&b=ab12cd345&c=x%2F${v}" and n == 2
    encoded, n = replace_value("t=ab%2B12%2Fcd%3D", "ab+12/cd=", "tok")
    assert encoded == "t=${__urlencode(${tok})}" and n == 1
    escaped, _ = replace_value('{"u": "ab12\\/cd34"}', "ab12/cd34", "u")
    assert escaped == '{"u": "${u}"}'


def test_boundaries_make_the_value_the_first_match():
    text = "<i value='zz'><input name='csrf' value='ab12cd34ef'>"
    left, right = _boundaries(text, text.index("ab12cd34ef"), "ab12cd34ef")
    start = text.find(left) + len(left)
    assert text[start:text.find(right, start)] == "ab12cd34ef"


def test_only_dynamic_looking_values_are_candidates():
    assert looks_dynamic("ab12cd34") and looks_dynamic("3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b")
    assert looks_dynamic("1700000000123")
    assert not looks_dynamic("password") and not looks_dynamic("1234") and not looks_dynamic("hello world 12")
    assert not looks_dynamic("https://x.test/a1b2c3")
