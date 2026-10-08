"""The plan a script's design produces: correlation, parameters, checks, run-time settings."""
import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from app.models.script import FilterRules, ScriptDesign
from app.services import script_builder
from app.services.har_parser import parse_har
from app.services.jmx_importer import parse_jmx
from app.services.request_filter import evaluate
from tests.recordings import SHOP_HAR, SHOP_JMX


def make(design=None, source="har", raw=None):
    raw = raw or (SHOP_HAR if source == "har" else SHOP_JMX)
    parsed = parse_har(raw) if source == "har" else parse_jmx(raw)
    script = {"id": "s_t", "name": "Shop", "source": source, "items": parsed["items"],
              "variables": parsed["variables"], "design": ScriptDesign(**(design or {})).model_dump(),
              "original": raw if source == "jmx" else None}
    return script, evaluate(source, parsed["items"], FilterRules(), {})


def build(design=None, **kw):
    source = kw.pop("source", "har")
    script, view = make(design, source)
    built = script_builder.build(script, view, **kw)
    return built, ET.fromstring(built.xml.encode())


def sampler_tree(root, label):
    for tree in root.iter("hashTree"):
        children = list(tree)
        for i, el in enumerate(children):
            if el.tag == "HTTPSamplerProxy" and el.get("testname").endswith(label):
                return children[i + 1]
    raise AssertionError(f"no sampler {label}")


def props(el):
    return {c.get("name"): c.text for c in el.iter() if c.get("name")}


def thread_group_tree(root):
    parent = root.find(".//ThreadGroup/..")
    children = list(parent)
    return children[children.index(parent.find("ThreadGroup")) + 1]


LOGIN = "POST /api/v1/auth/login"
SESSION = "GET /api/v1/auth/session"


def test_correlation_adds_an_extractor_to_the_source_request_and_resolves_the_placeholder():
    rule = {"variable": "authorization", "source": 5, "extractor": "json", "expression": "$.token"}
    built, root = build({"correlations": [rule]})
    extractor = sampler_tree(root, LOGIN).find("JSONPostProcessor")
    assert props(extractor) == {"JSONPostProcessor.referenceNames": "authorization",
                                "JSONPostProcessor.jsonPathExprs": "$.token",
                                "JSONPostProcessor.match_numbers": "1",
                                "JSONPostProcessor.defaultValues": "authorization_NOT_FOUND"}
    # the credential placeholder is no longer an empty User Defined Variable
    assert root.find(".//TestPlan/elementProp/collectionProp/elementProp") is None
    assert built.unresolved == {} and built.empty == []


def test_boundary_and_regex_extractors():
    rules = [{"variable": "csrf", "source": 0, "extractor": "boundary", "expression": "name='csrf' value='",
              "right": "'", "match": 2},
             {"variable": "code", "source": 0, "extractor": "regex", "scope": "headers",
              "expression": "(?i)location: .*code=(\\w+)"}]
    _, root = build({"correlations": rules})
    tree = sampler_tree(root, "GET /")
    assert props(tree.find("BoundaryExtractor")) | {} == {
        "BoundaryExtractor.useHeaders": "false", "BoundaryExtractor.refname": "csrf",
        "BoundaryExtractor.lboundary": "name='csrf' value='", "BoundaryExtractor.rboundary": "'",
        "BoundaryExtractor.default": "csrf_NOT_FOUND", "BoundaryExtractor.default_empty_value": "false",
        "BoundaryExtractor.match_number": "2"}
    regex = props(tree.find("RegexExtractor"))
    assert regex["RegexExtractor.useHeaders"] == "true" and regex["RegexExtractor.template"] == "$1$"


def test_manual_correlation_replaces_recorded_text_only_after_its_source():
    rule = {"variable": "prod", "source": 7, "extractor": "json", "expression": "$.items[0].id", "replace": "12345"}
    _, root = build({"correlations": [rule]})
    product = next(s for s in root.iter("HTTPSamplerProxy") if s.get("testname") == "GET /api/products/12345")
    assert props(product)["HTTPSampler.path"] == "/api/products/${prod}"


def test_parameters_become_data_sets_generators_and_functions():
    design = {
        "parameters": [
            {"name": "city", "type": "list", "values": ["Paris", "Oslo, NO"]},
            {"name": "color", "type": "list", "values": ["red", "blue"], "selection": "random"},
            {"name": "qty", "type": "random_number", "minimum": 1, "maximum": 9},
            {"name": "n", "type": "random_number", "minimum": 5, "maximum": 6, "update": "occurrence"},
            {"name": "seq", "type": "unique_number", "start": 100, "format": "000000"},
            {"name": "uid", "type": "uuid"},
            {"name": "day", "type": "date", "format": "yyyy-MM-dd, EEE", "offset_days": 2},
            {"name": "rs", "type": "random_string", "length": 6},
        ],
        "replacements": [{"find": "demo-pass", "variable": "uid"}, {"find": "demo", "variable": "city"}],
    }
    built, root = build(design)
    assert built.files["data/city.csv"] == b'Paris\n"Oslo, NO"\n'
    csv_set = props(next(e for e in root.iter("CSVDataSet")))
    assert csv_set["filename"] == "data/city.csv" and csv_set["variableNames"] == "city"
    assert csv_set["shareMode"] == "shareMode.all" and csv_set["ignoreFirstLine"] == "false"
    randoms = {props(e)["variableName"]: props(e) for e in root.iter("RandomVariableConfig")}
    assert randoms["qty"]["minimumValue"] == "1" and randoms["color__index"]["maximumValue"] == "2"
    counter = props(next(root.iter("CounterConfig")))
    assert counter["CounterConfig.name"] == "seq" and counter["CounterConfig.start"] == "100"
    variables = {e.get("name"): props(e)["Argument.value"] or "" for e in root.find(".//TestPlan").iter("elementProp")
                 if e.get("elementType") == "Argument"}
    assert variables == {"authorization": "", "color__1": "red", "color__2": "blue"}
    login = next(s for s in root.iter("HTTPSamplerProxy") if s.get("testname") == LOGIN)
    assert props(login)["Argument.value"] == '{"user": "${city}", "password": "${__UUID()}"}'
    assert script_builder._function(ScriptDesign(**design).parameters[6]) == "${__timeShift(yyyy-MM-dd\\, EEE,,P2D,,)}"
    assert script_builder._function(ScriptDesign(**design).parameters[3]) == "${__Random(5,6,)}"
    assert script_builder._function(ScriptDesign(**design).parameters[1]) == "${__V(color__${color__index})}"
    assert built.unresolved == {}


def test_checks_become_assertions_on_one_or_every_request():
    checks = [
        {"item": 5, "type": "text", "value": "token"},
        {"item": 5, "type": "not_text", "value": "error"},
        {"item": 5, "type": "regex", "value": "tok.n"},
        {"item": 5, "type": "header", "value": "application/json"},
        {"item": 5, "type": "json_path", "path": "$.token"},
        {"item": 5, "type": "json_path", "path": "$.status", "expected": "ok"},
        {"type": "duration", "limit": 800},
        {"item": 5, "type": "size_max", "limit": 5000},
    ]
    _, root = build({"checks": checks})
    tree = sampler_tree(root, LOGIN)
    kinds = [(props(a)["Assertion.test_field"], props(a)["Assertion.test_type"])
             for a in tree.findall("ResponseAssertion") if props(a).get("Assertion.test_field") != "Assertion.response_code"]
    assert kinds == [("Assertion.response_data", "16"), ("Assertion.response_data", "20"),
                     ("Assertion.response_data", "2"), ("Assertion.response_headers", "16")]
    json_checks = [props(a) for a in tree.findall("JSONPathAssertion")]
    assert json_checks[0]["JSONVALIDATION"] == "false" and json_checks[1]["EXPECTED_VALUE"] == "ok"
    assert props(tree.find("SizeAssertion"))["SizeAssertion.operator"] == "6"
    assert len(root.findall(".//DurationAssertion")) == 5          # the "every request" check
    with pytest.raises(ValidationError):
        ScriptDesign(checks=[{"type": "text"}])


def test_think_time_pacing_and_error_handling():
    settings = {"think_time": {"mode": "fixed", "seconds": 2}, "pacing": {"mode": "interval", "seconds": 30},
                "on_error": "next_iteration"}
    _, root = build({"settings": settings})
    tg = root.find(".//ThreadGroup")
    assert props(tg)["ThreadGroup.on_sample_error"] == "startnextloop"
    order = [(el.tag, el.get("testname")) for el in thread_group_tree(root) if el.tag != "hashTree"]
    assert order == [("TestAction", "Pacing"), ("TransactionController", "01 Home"), ("TestAction", "Think time"),
                     ("TransactionController", "02 Auth Login"), ("TestAction", "Think time"),
                     ("TransactionController", "03 Products Search")]
    pacing = props(root.find(".//ConstantThroughputTimer"))
    assert pacing == {"calcMode": "0", "throughput": "2.0"}
    assert {props(t)["ConstantTimer.delay"] for t in root.iter("ConstantTimer")} == {"2000"}


def test_recorded_think_time_is_capped_and_random_pacing_waits_after_the_iteration():
    _, root = build({"settings": {"think_time": {"mode": "recorded", "cap_seconds": 3},
                                  "pacing": {"mode": "after_random", "minimum": 1, "maximum": 4}}})
    delays = [props(t)["ConstantTimer.delay"] for t in root.iter("ConstantTimer")]
    assert delays == ["3000", "3000"]                         # recorded pauses of ~10 s, capped at 3 s
    tg_tree = thread_group_tree(root)
    assert tg_tree[-2].get("testname") == "Pacing"
    assert props(tg_tree[-1].find("UniformRandomTimer")) == {"ConstantTimer.delay": "1000", "RandomTimer.range": "3000"}


def test_debug_mode_is_one_user_with_labelled_requests_and_variable_snapshots():
    design = {"correlations": [{"variable": "tok", "source": 5, "extractor": "json", "expression": "$.token"}],
              "settings": {"think_time": {"mode": "fixed", "seconds": 5}, "pacing": {"mode": "interval", "seconds": 9}}}
    _, root = build(design, debug=True, users=50)
    tg = props(root.find(".//ThreadGroup"))
    assert tg["ThreadGroup.num_threads"] == "1" and tg["LoopController.loops"] == "1"
    assert [s.get("testname") for s in root.iter("HTTPSamplerProxy")][:2] == ["#0 GET /", f"#5 {LOGIN}"]
    assert [d.get("testname") for d in root.iter("DebugSampler")] == ["[variables after #5]"]
    assert root.find(".//TestAction") is None                  # no think time or pacing in a replay


def test_unresolved_and_empty_variables_are_reported():
    built, _ = build({"replacements": [{"find": "12345", "variable": "product_id"}]})
    assert built.unresolved == {"product_id": [8]}
    assert built.empty == ["authorization"]                     # recorded credential, no value yet
    assert built.hosts == ["shop.test"]


def test_the_same_design_always_builds_the_same_plan():
    design = {"checks": [{"item": 5, "type": "text", "value": "token"}],
              "parameters": [{"name": "c", "type": "list", "values": ["a"]}]}
    assert build(design)[0].xml == build(design)[0].xml


def test_design_applies_to_an_imported_jmx():
    design = {"correlations": [{"variable": "tok", "source": 2, "extractor": "json", "expression": "$.token"}],
              "parameters": [{"name": "section", "type": "list", "values": ["api"]}],
              "replacements": [{"find": "api", "variable": "section"}],
              "checks": [{"item": 2, "type": "text", "value": "token"}],
              "settings": {"think_time": {"mode": "ignore"}, "on_error": "stop_user"}}
    built, root = build(design, source="jmx")
    login = sampler_tree(root, "login")
    assert [el.tag for el in login if el.tag != "hashTree"] == ["JSONPostProcessor", "JSONPostProcessor", "ResponseAssertion"]
    assert next(s for s in root.iter("HTTPSamplerProxy") if s.get("testname") == "login").find(
        "stringProp[@name='HTTPSampler.path']").text == "/${section}/login"
    assert root.find(".//ConstantTimer") is None                # think time ignored: the recorded timer is gone
    assert props(root.find(".//ThreadGroup"))["ThreadGroup.on_sample_error"] == "stopthread"
    assert props(root.find(".//ThreadGroup"))["ThreadGroup.num_threads"] == "7"   # export keeps its users
    assert built.unresolved == {}
