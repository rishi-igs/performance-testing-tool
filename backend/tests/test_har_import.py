import json
import xml.etree.ElementTree as ET

import pytest

from app.models.script import FilterRules
from app.services.har_parser import ImportFailed, parse_har
from app.services.jmeter_plan_builder import build_recorded_plan
from app.services.request_filter import auto_reason, evaluate, name_from_url
from tests.recordings import SHOP_HAR, entry, har


def shop(rules=None, overrides=None):
    parsed = parse_har(SHOP_HAR)
    return parsed, evaluate("har", parsed["items"], rules or FilterRules(), overrides or {})


def test_static_third_party_and_browser_noise_are_excluded_with_reasons():
    _, view = shop()
    reasons = {v["index"]: v["auto_reason"] for v in view["items"]}
    assert reasons[0] is None and reasons[5] is None and reasons[6] is None
    assert reasons[1] == "Static file (.js)"
    assert reasons[2] == "Static file (.png)"
    assert reasons[3] == "Third-party domain (*.google-analytics.com)"
    assert reasons[4] == "Browser preflight (OPTIONS)"
    assert reasons[9] == "Not an HTTP request (browser-internal)"
    assert reasons[10].startswith("Cancelled or failed in the browser")
    assert view["summary"] == {"captured": 11, "kept": 5, "excluded": 6, "transactions": 3}


def test_requests_are_grouped_by_idle_gaps_and_named_from_the_url():
    _, view = shop()
    assert [(t["name"], t["items"]) for t in view["transactions"]] == [
        ("Home", [0]), ("Auth Login", [5, 6]), ("Products Search", [7, 8]),
    ]
    # The gap can be turned off: then the whole page is one business function
    _, view = shop(FilterRules(idle_gap_seconds=0))
    assert [t["name"] for t in view["transactions"]] == ["Home"]


def test_a_real_page_title_names_the_first_group_of_that_page():
    data = har(entry("https://shop.test/", start=0, page="p1"), entry("https://shop.test/api/x", start=100, page="p1"),
               pages=[{"id": "p1", "title": "Shop – Welcome"}])
    parsed = parse_har(data)
    view = evaluate("har", parsed["items"], FilterRules(), {})
    assert [t["name"] for t in view["transactions"]] == ["Shop – Welcome"]


def test_rules_and_manual_edits_take_priority_over_automatic_grouping():
    rules = FilterRules(transaction_rules=[{"name": "Login", "url_pattern": "*/auth/*"}])
    overrides = {"1": {"include": True, "transaction": "Home"}, "8": {"transaction": "Product details"},
                 "0": {"include": False}}
    _, view = shop(rules, overrides)
    items = view["items"]
    assert items[5]["transaction"] == "Login" and items[5]["transaction_source"] == "rule"
    assert items[1]["include"] and items[1]["include_source"] == "manual" and items[1]["auto_reason"]
    assert not items[0]["include"]
    assert [(t["name"], t["items"]) for t in view["transactions"]] == [
        ("Home", [1]), ("Login", [5, 6]), ("Products Search", [7]), ("Product details", [8]),
    ]


def test_keep_only_api_drops_page_navigations():
    _, view = shop(FilterRules(keep_only_api=True))
    assert view["items"][0]["auto_reason"] == "Not an API call"
    assert view["items"][5]["include"]


def test_custom_exclusions_and_domain_matching():
    rules = FilterRules(exclude_domains=["*.shop.test"], exclude_extensions=[".JSON"])
    item = {"kind": "http", "url": "https://shop.test/a", "host": "shop.test", "method": "GET"}
    assert auto_reason(item, rules) == "Third-party domain (*.shop.test)"   # *.x also matches x itself
    other = {**item, "url": "https://notshop.test/data.json", "host": "notshop.test"}
    assert auto_reason(other, rules) == "Static file (.json)"


@pytest.mark.parametrize("url,name", [
    ("https://x.test/api/v1/auth/login", "Auth Login"),
    ("https://x.test/products/12345", "Products"),
    ("https://x.test/", "Home"),
    ("https://x.test/checkout.html?step=2", "Checkout"),
    ("https://x.test/users/550e8400-e29b-41d4-a716-446655440000/orders", "Users Orders"),
    ("https://x.test/cart/add_item", "Cart Add Item"),
])
def test_name_from_url(url, name):
    assert name_from_url(url) == name


def test_recorded_secrets_are_not_kept():
    parsed = parse_har(SHOP_HAR)
    stored = json.dumps(parsed)
    assert "s3cret-token" not in stored and "s3cret-cookie" not in stored
    session = parsed["items"][6]
    assert ["Authorization", "Bearer ${authorization}"] in session["headers"]
    assert not any(h[0].lower() == "cookie" for i in parsed["items"] for h in i["headers"])
    assert parsed["variables"] == ["authorization"]
    assert any("${authorization}" in w for w in parsed["warnings"])
    assert any("cookies were removed" in w for w in parsed["warnings"])


@pytest.mark.parametrize("raw,message", [
    (b"not json", "could not be read as JSON"),
    (b'{"log": {}}', "no log.entries"),
    (b'{"log": {"entries": []}}', "no requests"),
    (b"[1, 2]", "no log.entries"),
])
def test_invalid_har_files_are_rejected_with_a_reason(raw, message):
    with pytest.raises(ImportFailed, match=message):
        parse_har(raw)


def test_form_params_become_a_body_and_entries_are_sorted_by_start_time():
    late = entry("https://shop.test/b", start=500)
    early = entry("https://shop.test/a", method="POST", start=0)
    early["request"]["postData"] = {"mimeType": "application/x-www-form-urlencoded",
                                    "params": [{"name": "q", "value": "a b"}, {"name": "n", "value": "1"}]}
    items = parse_har(har(late, early))["items"]
    assert [i["url"] for i in items] == ["https://shop.test/a", "https://shop.test/b"]
    assert items[0]["body"] == "q=a+b&n=1"
    assert ["Content-Type", "application/x-www-form-urlencoded"] in items[0]["headers"]


def test_binary_bodies_are_written_as_character_references_and_round_trip():
    from app.services.jmx_importer import parse_jmx, rebuild

    data = har(entry("https://shop.test/upload", method="POST", body="bin\u0010\u0000ary\u001b"))
    parsed = parse_har(data)
    view = evaluate("har", parsed["items"], FilterRules(), {})
    xml = build_recorded_plan("p", [(t["name"], [parsed["items"][i] for i in t["items"]]) for t in view["transactions"]])
    assert "bin&#x10;&#x0;ary&#x1b;" in xml and "\u0010" not in xml
    # JMeter-style references in an exported plan survive a JMX import and export
    again = parse_jmx(xml.encode())
    out = rebuild(xml.encode(), evaluate("jmx", again["items"], FilterRules(), {})["transactions"])
    assert "bin&#x10;&#x0;ary&#x1b;" in out


def test_recorded_plan_has_jmeter_structure():
    parsed, view = shop()
    groups = [(t["name"], [parsed["items"][i] for i in t["items"]]) for t in view["transactions"]]
    root = ET.fromstring(build_recorded_plan("Shop <flow>", groups, variables=parsed["variables"], users=3, loops=2))

    plan_tree = root.find("hashTree/hashTree")
    top_level = [el.tag for el in plan_tree if el.tag != "hashTree"]
    assert top_level == ["ConfigTestElement", "CookieManager", "HeaderManager", "ThreadGroup", "ResultCollector"]
    assert root.find(".//ResultCollector").get("enabled") == "false"
    assert root.find(".//TestPlan").get("testname") == "Shop <flow>"
    udv = root.find(".//TestPlan/elementProp/collectionProp/elementProp")
    assert udv.get("name") == "authorization"

    tg = root.find(".//ThreadGroup")
    assert tg.find("stringProp[@name='ThreadGroup.num_threads']").text == "3"
    assert tg.find(".//stringProp[@name='LoopController.loops']").text == "2"
    assert [tc.get("testname") for tc in root.iter("TransactionController")] == [
        "01 Home", "02 Auth Login", "03 Products Search"]

    # Shared headers sit once at plan level; per-request headers stay on the request
    common = plan_tree.find("HeaderManager")
    assert common.get("testname") == "Common headers"
    assert [e.text for e in common.iter("stringProp")] == ["User-Agent", "test-browser"]

    samplers = list(root.iter("HTTPSamplerProxy"))
    assert [s.get("testname") for s in samplers] == [
        "GET /", "POST /api/v1/auth/login", "GET /api/v1/auth/session",
        "GET /api/products/search", "GET /api/products/12345"]
    login = samplers[1]
    assert login.find("stringProp[@name='HTTPSampler.domain']").text == "shop.test"
    assert login.find("stringProp[@name='HTTPSampler.port']").text == "443"
    assert login.find("boolProp[@name='HTTPSampler.follow_redirects']").text == "false"
    assert login.find(".//stringProp[@name='Argument.value']").text == '{"user": "demo", "password": "demo-pass"}'
    assert login.find("stringProp[@name='HTTPSampler.contentEncoding']").text == "UTF-8"
    assert samplers[3].find("stringProp[@name='HTTPSampler.path']").text == "/api/products/search?q=phone"

    assertion = root.find(".//ResponseAssertion")
    assert assertion.find("boolProp[@name='Assertion.assume_success']").text == "true"
    assert [e.text for e in assertion.find("collectionProp")] == ["200"]
