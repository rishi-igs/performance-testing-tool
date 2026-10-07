import xml.etree.ElementTree as ET

from app.models.test_config import LoadTestConfig
from app.services.jmeter_plan_builder import build_plan


def plan(**kw):
    cfg = LoadTestConfig(**{"name": "My <plan> & test", "target_url": "https://api.example.com/checkout?x=1&y=2", **kw})
    return ET.fromstring(build_plan(cfg).encode())


def prop(root, name):
    return next(e.text for e in root.iter() if e.get("name") == name)


def test_valid_xml_with_expected_structure():
    root = plan(users=25, duration_seconds=120, ramp_up_seconds=30)
    assert root.tag == "jmeterTestPlan"
    assert prop(root, "ThreadGroup.num_threads") == "25"
    assert prop(root, "ThreadGroup.ramp_time") == "30"
    assert prop(root, "ThreadGroup.duration") == "120"
    assert prop(root, "ThreadGroup.scheduler") == "true"


def test_sampler_fields_come_from_url():
    root = plan(method="POST")
    assert prop(root, "HTTPSampler.domain") == "api.example.com"
    assert prop(root, "HTTPSampler.port") == "443"
    assert prop(root, "HTTPSampler.protocol") == "https"
    assert prop(root, "HTTPSampler.path") == "/checkout?x=1&y=2"
    assert prop(root, "HTTPSampler.method") == "POST"


def test_special_characters_are_escaped_not_injected():
    root = plan(headers={"X-Note": "a<b>&\"c\""}, body='{"q": "<script>&"}')
    assert prop(root, "Header.value") == 'a<b>&"c"'
    assert prop(root, "Argument.value") == '{"q": "<script>&"}'
    assert root.find(".//TestPlan").get("testname") == "My <plan> & test"


def test_expected_status_codes_and_optional_parts():
    root = plan(expected_status_codes=[200, 201])
    codes = [e.text for e in root.find(".//ResponseAssertion/collectionProp")]
    assert codes == ["200", "201"]
    assert root.find(".//HeaderManager") is None      # no headers configured
    assert root.find(".//ConstantTimer") is None      # no think time
    assert plan(think_time_ms=250).find(".//ConstantTimer") is not None


def test_no_body_means_no_raw_body_flag():
    assert all(e.get("name") != "HTTPSampler.postBodyRaw" for e in plan().iter())
