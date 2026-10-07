"""Generate a JMeter .jmx test plan from a LoadTestConfig (Phase 1: one request)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from ..models.test_config import LoadTestConfig

JMETER_VERSION = "5.6.3"

# JMeter ResponseAssertion test_type bits: Equals (8) + Or (32)
_EQUALS_OR = 40


def _prop(parent: ET.Element, tag: str, name: str, value: object, **extra: str) -> ET.Element:
    el = ET.SubElement(parent, tag, {"name": name, **extra})
    el.text = str(value) if not isinstance(value, bool) else str(value).lower()
    return el


def _element(parent_tree: ET.Element, tag: str, guiclass: str, testclass: str, testname: str) -> tuple[ET.Element, ET.Element]:
    """Append <tag/> and its sibling <hashTree/>; return (element, child hashTree)."""
    el = ET.SubElement(
        parent_tree, tag,
        {"guiclass": guiclass, "testclass": testclass, "testname": testname, "enabled": "true"},
    )
    return el, ET.SubElement(parent_tree, "hashTree")


def build_plan(config: LoadTestConfig) -> str:
    url = urlsplit(config.target_url)
    protocol = url.scheme
    port = url.port or (443 if protocol == "https" else 80)
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")

    root = ET.Element("jmeterTestPlan", {"version": "1.2", "properties": "5.0", "jmeter": JMETER_VERSION})
    top = ET.SubElement(root, "hashTree")

    # Test plan
    plan, plan_tree = _element(top, "TestPlan", "TestPlanGui", "TestPlan", config.name)
    _prop(plan, "boolProp", "TestPlan.functional_mode", False)
    _prop(plan, "boolProp", "TestPlan.tearDown_on_shutdown", True)
    _prop(plan, "boolProp", "TestPlan.serialize_threadgroups", False)
    args = ET.SubElement(plan, "elementProp", {
        "name": "TestPlan.user_defined_variables", "elementType": "Arguments",
        "guiclass": "ArgumentsPanel", "testclass": "Arguments",
        "testname": "User Defined Variables", "enabled": "true",
    })
    ET.SubElement(args, "collectionProp", {"name": "Arguments.arguments"})

    # Thread group: N users, ramp-up, run for a fixed duration (scheduler)
    tg, tg_tree = _element(plan_tree, "ThreadGroup", "ThreadGroupGui", "ThreadGroup", "Virtual users")
    _prop(tg, "stringProp", "ThreadGroup.on_sample_error", "continue")
    loop = ET.SubElement(tg, "elementProp", {
        "name": "ThreadGroup.main_controller", "elementType": "LoopController",
        "guiclass": "LoopControlPanel", "testclass": "LoopController",
        "testname": "Loop Controller", "enabled": "true",
    })
    _prop(loop, "boolProp", "LoopController.continue_forever", False)
    _prop(loop, "intProp", "LoopController.loops", -1)
    _prop(tg, "stringProp", "ThreadGroup.num_threads", config.users)
    _prop(tg, "stringProp", "ThreadGroup.ramp_time", config.ramp_up_seconds)
    _prop(tg, "boolProp", "ThreadGroup.scheduler", True)
    _prop(tg, "stringProp", "ThreadGroup.duration", config.duration_seconds)
    _prop(tg, "stringProp", "ThreadGroup.delay", "")
    _prop(tg, "boolProp", "ThreadGroup.same_user_on_next_iteration", True)

    # HTTP sampler
    sampler, sampler_tree = _element(
        tg_tree, "HTTPSamplerProxy", "HttpTestSampleGui", "HTTPSamplerProxy",
        f"{config.method} {path}",
    )
    sampler_args = ET.SubElement(sampler, "elementProp", {
        "name": "HTTPsampler.Arguments", "elementType": "Arguments",
        "guiclass": "HTTPArgumentsPanel", "testclass": "Arguments", "enabled": "true",
    })
    coll = ET.SubElement(sampler_args, "collectionProp", {"name": "Arguments.arguments"})
    if config.body is not None:
        _prop(sampler, "boolProp", "HTTPSampler.postBodyRaw", True)
        arg = ET.SubElement(coll, "elementProp", {"name": "", "elementType": "HTTPArgument"})
        _prop(arg, "boolProp", "HTTPArgument.always_encode", False)
        _prop(arg, "stringProp", "Argument.value", config.body)
        _prop(arg, "stringProp", "Argument.metadata", "=")
    _prop(sampler, "stringProp", "HTTPSampler.domain", url.hostname or "")
    _prop(sampler, "stringProp", "HTTPSampler.port", port)
    _prop(sampler, "stringProp", "HTTPSampler.protocol", protocol)
    _prop(sampler, "stringProp", "HTTPSampler.path", path)
    _prop(sampler, "stringProp", "HTTPSampler.method", config.method)
    _prop(sampler, "boolProp", "HTTPSampler.follow_redirects", True)
    _prop(sampler, "boolProp", "HTTPSampler.auto_redirects", False)
    _prop(sampler, "boolProp", "HTTPSampler.use_keepalive", True)
    _prop(sampler, "stringProp", "HTTPSampler.connect_timeout", config.timeout_ms)
    _prop(sampler, "stringProp", "HTTPSampler.response_timeout", config.timeout_ms)

    # Headers
    if config.headers:
        hm, hm_tree = _element(sampler_tree, "HeaderManager", "HeaderPanel", "HeaderManager", "HTTP Header Manager")
        hcoll = ET.SubElement(hm, "collectionProp", {"name": "HeaderManager.headers"})
        for name, value in config.headers.items():
            h = ET.SubElement(hcoll, "elementProp", {"name": "", "elementType": "Header"})
            _prop(h, "stringProp", "Header.name", name)
            _prop(h, "stringProp", "Header.value", value)

    # Expected status codes
    ra, _ = _element(sampler_tree, "ResponseAssertion", "AssertionGui", "ResponseAssertion", "Expected status code")
    codes = ET.SubElement(ra, "collectionProp", {"name": "Asserion.test_strings"})  # sic: JMeter's spelling
    for code in config.expected_status_codes:
        _prop(codes, "stringProp", str(code), code)
    _prop(ra, "stringProp", "Assertion.custom_message", "")
    _prop(ra, "stringProp", "Assertion.test_field", "Assertion.response_code")
    _prop(ra, "boolProp", "Assertion.assume_success", False)
    _prop(ra, "intProp", "Assertion.test_type", _EQUALS_OR)

    # Think time
    if config.think_time_ms:
        timer, _ = _element(sampler_tree, "ConstantTimer", "ConstantTimerGui", "ConstantTimer", "Think time")
        _prop(timer, "stringProp", "ConstantTimer.delay", config.think_time_ms)

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def jmeter_properties() -> list[str]:
    """-J properties so the results file has the columns the analyzer reads."""
    return [
        "-Jjmeter.save.saveservice.output_format=csv",
        "-Jjmeter.save.saveservice.timestamp_format=ms",
        "-Jjmeter.save.saveservice.thread_counts=true",
        "-Jjmeter.save.saveservice.response_data.on_error=false",
        "-Jjmeter.save.saveservice.assertion_results_failure_message=true",
    ]
