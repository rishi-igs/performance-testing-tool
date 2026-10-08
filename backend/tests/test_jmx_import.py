import xml.etree.ElementTree as ET

import pytest

from app.models.script import FilterRules
from app.services.har_parser import ImportFailed
from app.services.jmx_importer import parse_jmx, rebuild
from app.services.request_filter import evaluate
from tests.recordings import SHOP_JMX


def view(overrides=None, rules=None):
    parsed = parse_jmx(SHOP_JMX)
    return parsed, evaluate("jmx", parsed["items"], rules or FilterRules(), overrides or {})


def children(tree):
    return [(el.tag, el.get("testname")) for el in tree if el.tag != "hashTree"]


def test_samplers_are_listed_with_urls_from_http_defaults():
    parsed, _ = view()
    assert parsed["name"] == "Shop flow"
    items = parsed["items"]
    assert [(i["kind"], i["label"]) for i in items] == [
        ("http", "home"), ("http", "logo"), ("http", "login"),
        ("other", "Maybe promo (IfController)"), ("http", "old page"),
    ]
    assert items[2]["url"] == "https://shop.test/api/login" and items[2]["method"] == "POST"
    assert items[4]["disabled"]
    assert any("logic controller" in w for w in parsed["warnings"])


def test_existing_controllers_become_business_functions():
    _, v = view()
    reasons = [i["auto_reason"] for i in v["items"]]
    assert reasons == [None, "Static file (.png)", None, None, "Disabled in the imported plan"]
    assert [(t["name"], t["items"]) for t in v["transactions"]] == [("Home page", [0]), ("Login", [2, 3])]


def test_rebuild_regroups_and_keeps_each_request_with_its_children():
    _, v = view()
    root = ET.fromstring(rebuild(SHOP_JMX, v["transactions"]))
    plan_tree = root.find("hashTree/hashTree")
    assert children(plan_tree) == [
        ("ConfigTestElement", "HTTP Request Defaults"), ("ThreadGroup", "Users"), ("ResultCollector", "Summary Report"),
    ]
    tg_tree = plan_tree[3]
    assert children(tg_tree) == [
        ("HeaderManager", "Thread group headers"),
        ("TransactionController", "01 Home page"),
        ("TransactionController", "02 Login"),
    ]
    assert root.find(".//ThreadGroup/stringProp").text == "7"           # thread group settings untouched
    home, login = tg_tree[3], tg_tree[5]
    assert children(home) == [("HTTPSamplerProxy", "home")]
    # The timer that was scoped to the old Login controller moves with its requests;
    # the extractor stays under its sampler; the If Controller stays one block.
    assert children(login) == [
        ("ConstantTimer", "Login think time"), ("HTTPSamplerProxy", "login"), ("IfController", "Maybe promo"),
    ]
    assert children(login[3]) == [("JSONPostProcessor", "Extract token")]
    assert children(login[5]) == [("HTTPSamplerProxy", "promo")]
    for gone in ("RecordingController", "GenericController"):
        assert root.find(f".//{gone}") is None


def test_exported_plan_can_be_imported_again_without_doubling_numbers():
    _, v = view()
    exported = rebuild(SHOP_JMX, v["transactions"]).encode()
    parsed = parse_jmx(exported)
    again = evaluate("jmx", parsed["items"], FilterRules(), {})
    assert [t["name"] for t in again["transactions"]] == ["Home page", "Login"]
    root = ET.fromstring(rebuild(exported, again["transactions"]))
    assert [tc.get("testname") for tc in root.iter("TransactionController")] == ["01 Home page", "02 Login"]


def test_re_including_a_disabled_sampler_enables_it():
    _, v = view({"4": {"include": True}})
    assert v["transactions"][-1]["name"] == "Old"
    root = ET.fromstring(rebuild(SHOP_JMX, v["transactions"]))
    old = next(s for s in root.iter("HTTPSamplerProxy") if s.get("testname") == "old page")
    assert old.get("enabled") == "true"


def test_control_character_references_written_by_jmeter_survive_import_and_export():
    # JMeter writes control characters from recorded binary bodies as &#x10; etc., which XML 1.0 forbids.
    body = "<stringProp name=\"Argument.value\">a&#x10;b&#x0;c&#27;d&#x9;e&#xFFFF;</stringProp>"
    raw = SHOP_JMX.replace(
        b'<stringProp name="HTTPSampler.path">/api/login</stringProp>',
        f'<stringProp name="HTTPSampler.path">/api/login</stringProp>{body}'.encode(),
    ).replace(b'testname="promo"', b'testname="promo&#x1b;x"')
    parsed = parse_jmx(raw)
    assert parsed["items"][3]["label"] == "Maybe promo (IfController)"
    promo_label = parse_jmx(raw.replace(b'testname="Maybe promo"', b'testname="Maybe&#x1;promo"'))["items"][3]["label"]
    assert promo_label == "Maybe\\x01promo (IfController)"       # shown readably on the review screen

    v = evaluate("jmx", parsed["items"], FilterRules(), {})
    exported = rebuild(raw, v["transactions"])
    for ref in ("&#x10;", "&#x0;", "&#x1b;", "&#xffff;"):
        assert ref in exported
    assert "&#27;" not in exported and "" not in exported    # decimal refs come back as hex refs
    again = parse_jmx(exported.encode())                         # and the export can be imported again
    assert [i["label"] for i in again["items"]] == [i["label"] for i in v["items"] if i["include"]]


@pytest.mark.parametrize("raw,message", [
    (b"<notjmeter/>", "root element is <notjmeter>"),
    (b"<jmeterTestPlan><hashTree/></jmeterTestPlan>", "no Test Plan"),
    (b"<jmeterTestPlan", "not a valid .jmx"),
    (b'<!DOCTYPE x [<!ENTITY a "aaaa">]><jmeterTestPlan/>', "DOCTYPE"),
])
def test_invalid_jmx_files_are_rejected(raw, message):
    with pytest.raises(ImportFailed, match=message):
        parse_jmx(raw)
