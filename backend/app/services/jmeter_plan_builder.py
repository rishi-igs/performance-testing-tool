"""Generate a JMeter .jmx test plan from a LoadTestConfig (one request)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import SplitResult, urlsplit

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


def _add_thread_group(
    parent_tree: ET.Element,
    config: LoadTestConfig,
    *,
    users: int,
    ramp_time: int,
    duration: int,
    delay: int | str,
    group_name: str,
    sampler_name: str,
    url_parts: SplitResult,
    port: int,
) -> None:
    tg, tg_tree = _element(parent_tree, "ThreadGroup", "ThreadGroupGui", "ThreadGroup", group_name)
    _prop(tg, "stringProp", "ThreadGroup.on_sample_error", "continue")
    loop = ET.SubElement(tg, "elementProp", {
        "name": "ThreadGroup.main_controller", "elementType": "LoopController",
        "guiclass": "LoopControlPanel", "testclass": "LoopController",
        "testname": "Loop Controller", "enabled": "true",
    })
    _prop(loop, "boolProp", "LoopController.continue_forever", False)
    _prop(loop, "intProp", "LoopController.loops", -1)
    _prop(tg, "stringProp", "ThreadGroup.num_threads", users)
    _prop(tg, "stringProp", "ThreadGroup.ramp_time", ramp_time)
    _prop(tg, "boolProp", "ThreadGroup.scheduler", True)
    _prop(tg, "stringProp", "ThreadGroup.duration", duration)
    _prop(tg, "stringProp", "ThreadGroup.delay", delay)
    _prop(tg, "boolProp", "ThreadGroup.same_user_on_next_iteration", True)

    sampler, sampler_tree = _element(
        tg_tree, "HTTPSamplerProxy", "HttpTestSampleGui", "HTTPSamplerProxy", sampler_name,
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
    _prop(sampler, "stringProp", "HTTPSampler.domain", url_parts.hostname or "")
    _prop(sampler, "stringProp", "HTTPSampler.port", port)
    _prop(sampler, "stringProp", "HTTPSampler.protocol", url_parts.scheme)
    _prop(sampler, "stringProp", "HTTPSampler.path", (url_parts.path or "/") + (f"?{url_parts.query}" if url_parts.query else ""))
    _prop(sampler, "stringProp", "HTTPSampler.method", config.method)
    _prop(sampler, "boolProp", "HTTPSampler.follow_redirects", True)
    _prop(sampler, "boolProp", "HTTPSampler.auto_redirects", False)
    _prop(sampler, "boolProp", "HTTPSampler.use_keepalive", True)
    _prop(sampler, "stringProp", "HTTPSampler.connect_timeout", config.timeout_ms)
    _prop(sampler, "stringProp", "HTTPSampler.response_timeout", config.timeout_ms)

    if config.headers:
        hm, hm_tree = _element(sampler_tree, "HeaderManager", "HeaderPanel", "HeaderManager", "HTTP Header Manager")
        hcoll = ET.SubElement(hm, "collectionProp", {"name": "HeaderManager.headers"})
        for name, value in config.headers.items():
            h = ET.SubElement(hcoll, "elementProp", {"name": "", "elementType": "Header"})
            _prop(h, "stringProp", "Header.name", name)
            _prop(h, "stringProp", "Header.value", value)

    ra, _ = _element(sampler_tree, "ResponseAssertion", "AssertionGui", "ResponseAssertion", "Expected status code")
    codes = ET.SubElement(ra, "collectionProp", {"name": "Asserion.test_strings"})  # sic: JMeter's spelling
    for code in config.expected_status_codes:
        _prop(codes, "stringProp", str(code), code)
    _prop(ra, "stringProp", "Assertion.custom_message", "")
    _prop(ra, "stringProp", "Assertion.test_field", "Assertion.response_code")
    _prop(ra, "boolProp", "Assertion.assume_success", False)
    _prop(ra, "intProp", "Assertion.test_type", _EQUALS_OR)

    if config.think_time_ms:
        timer, _ = _element(sampler_tree, "ConstantTimer", "ConstantTimerGui", "ConstantTimer", "Think time")
        _prop(timer, "stringProp", "ConstantTimer.delay", config.think_time_ms)


def build_plan(config: LoadTestConfig) -> str:
    url = urlsplit(config.target_url)
    port = url.port or (443 if url.scheme == "https" else 80)

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

    sampler_name = f"{config.method} {(url.path or '/') + (f'?{url.query}' if url.query else '')}"
    if config.profile is None:
        cohorts = [(config.users, "", config.duration_seconds, "Virtual users", config.ramp_up_seconds)]
    else:
        cohorts = []
        previous_users = 0
        for index, step in enumerate(config.profile):
            cohorts.append((
                step.users - previous_users,
                step.time_seconds,
                config.duration_seconds - step.time_seconds,
                f"Step {index + 1}: {step.users} users",
                0,
            ))
            previous_users = step.users

    for users, delay, duration, group_name, ramp_time in cohorts:
        _add_thread_group(
            plan_tree,
            config,
            users=users,
            ramp_time=ramp_time,
            duration=duration,
            delay=delay,
            group_name=group_name,
            sampler_name=sampler_name,
            url_parts=url,
            port=port,
        )

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
