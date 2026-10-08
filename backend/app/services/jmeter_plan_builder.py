"""Generate JMeter .jmx test plans: one request from a LoadTestConfig, or a recorded flow."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable
from urllib.parse import SplitResult, urlsplit

from ..models.test_config import LoadTestConfig

JMETER_VERSION = "5.6.3"

# JMeter ResponseAssertion test_type bits: Equals (8) + Or (32)
_EQUALS_OR = 40
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


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


def _add_http_sampler(
    parent_tree: ET.Element,
    name: str,
    *,
    url_parts: SplitResult,
    port: int,
    method: str,
    body: str | None,
    follow_redirects: bool = True,
    timeout_ms: int | None = None,
    content_encoding: str | None = None,
) -> ET.Element:
    """Append an HTTP Request sampler; return its child hashTree."""
    sampler, sampler_tree = _element(
        parent_tree, "HTTPSamplerProxy", "HttpTestSampleGui", "HTTPSamplerProxy", name,
    )
    sampler_args = ET.SubElement(sampler, "elementProp", {
        "name": "HTTPsampler.Arguments", "elementType": "Arguments",
        "guiclass": "HTTPArgumentsPanel", "testclass": "Arguments", "enabled": "true",
    })
    coll = ET.SubElement(sampler_args, "collectionProp", {"name": "Arguments.arguments"})
    if body is not None:
        _prop(sampler, "boolProp", "HTTPSampler.postBodyRaw", True)
        arg = ET.SubElement(coll, "elementProp", {"name": "", "elementType": "HTTPArgument"})
        _prop(arg, "boolProp", "HTTPArgument.always_encode", False)
        _prop(arg, "stringProp", "Argument.value", body)
        _prop(arg, "stringProp", "Argument.metadata", "=")
    _prop(sampler, "stringProp", "HTTPSampler.domain", url_parts.hostname or "")
    _prop(sampler, "stringProp", "HTTPSampler.port", port)
    _prop(sampler, "stringProp", "HTTPSampler.protocol", url_parts.scheme)
    _prop(sampler, "stringProp", "HTTPSampler.path", (url_parts.path or "/") + (f"?{url_parts.query}" if url_parts.query else ""))
    _prop(sampler, "stringProp", "HTTPSampler.method", method)
    if content_encoding:
        _prop(sampler, "stringProp", "HTTPSampler.contentEncoding", content_encoding)
    _prop(sampler, "boolProp", "HTTPSampler.follow_redirects", follow_redirects)
    _prop(sampler, "boolProp", "HTTPSampler.auto_redirects", False)
    _prop(sampler, "boolProp", "HTTPSampler.use_keepalive", True)
    if timeout_ms is not None:
        _prop(sampler, "stringProp", "HTTPSampler.connect_timeout", timeout_ms)
        _prop(sampler, "stringProp", "HTTPSampler.response_timeout", timeout_ms)
    return sampler_tree


def _add_header_manager(parent_tree: ET.Element, headers: Iterable[tuple[str, str]], name: str = "HTTP Header Manager") -> None:
    hm, _ = _element(parent_tree, "HeaderManager", "HeaderPanel", "HeaderManager", name)
    hcoll = ET.SubElement(hm, "collectionProp", {"name": "HeaderManager.headers"})
    for header_name, value in headers:
        h = ET.SubElement(hcoll, "elementProp", {"name": "", "elementType": "Header"})
        _prop(h, "stringProp", "Header.name", header_name)
        _prop(h, "stringProp", "Header.value", value)


def _add_status_assertion(parent_tree: ET.Element, codes: Iterable[int], *, ignore_status: bool) -> None:
    """Response code must equal one of `codes`.

    ignore_status=True is JMeter's "Ignore Status" option: without it JMeter marks every 4xx/5xx
    response as failed before the assertion runs, even when that code is the expected one.
    """
    ra, _ = _element(parent_tree, "ResponseAssertion", "AssertionGui", "ResponseAssertion", "Expected status code")
    strings = ET.SubElement(ra, "collectionProp", {"name": "Asserion.test_strings"})  # sic: JMeter's spelling
    for code in codes:
        _prop(strings, "stringProp", str(code), code)
    _prop(ra, "stringProp", "Assertion.custom_message", "")
    _prop(ra, "stringProp", "Assertion.test_field", "Assertion.response_code")
    _prop(ra, "boolProp", "Assertion.assume_success", ignore_status)
    _prop(ra, "intProp", "Assertion.test_type", _EQUALS_OR)


def add_transaction_controller(parent_tree: ET.Element, name: str) -> ET.Element:
    """Append a Transaction Controller (one business function); return its child hashTree."""
    tc, tc_tree = _element(parent_tree, "TransactionController", "TransactionControllerGui", "TransactionController", name)
    _prop(tc, "boolProp", "TransactionController.parent", False)
    return tc_tree


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

    sampler_tree = _add_http_sampler(
        tg_tree, sampler_name, url_parts=url_parts, port=port, method=config.method,
        body=config.body, timeout_ms=config.timeout_ms,
    )
    if config.headers:
        _add_header_manager(sampler_tree, config.headers.items())
    _add_status_assertion(sampler_tree, config.expected_status_codes, ignore_status=False)

    if config.think_time_ms:
        timer, _ = _element(sampler_tree, "ConstantTimer", "ConstantTimerGui", "ConstantTimer", "Think time")
        _prop(timer, "stringProp", "ConstantTimer.delay", config.think_time_ms)


def build_plan(config: LoadTestConfig) -> str:
    url = urlsplit(config.target_url)
    port = url.port or (443 if url.scheme == "https" else 80)
    root, plan_tree = _test_plan(config.name)

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

    return serialize(root)


def _test_plan(name: str, variables: Iterable[str] = ()) -> tuple[ET.Element, ET.Element]:
    """Root element and Test Plan; `variables` become empty User Defined Variables. Returns (root, plan hashTree)."""
    root = ET.Element("jmeterTestPlan", {"version": "1.2", "properties": "5.0", "jmeter": JMETER_VERSION})
    top = ET.SubElement(root, "hashTree")
    plan, plan_tree = _element(top, "TestPlan", "TestPlanGui", "TestPlan", name)
    _prop(plan, "boolProp", "TestPlan.functional_mode", False)
    _prop(plan, "boolProp", "TestPlan.tearDown_on_shutdown", True)
    _prop(plan, "boolProp", "TestPlan.serialize_threadgroups", False)
    args = ET.SubElement(plan, "elementProp", {
        "name": "TestPlan.user_defined_variables", "elementType": "Arguments",
        "guiclass": "ArgumentsPanel", "testclass": "Arguments",
        "testname": "User Defined Variables", "enabled": "true",
    })
    coll = ET.SubElement(args, "collectionProp", {"name": "Arguments.arguments"})
    for var in variables:
        arg = ET.SubElement(coll, "elementProp", {"name": var, "elementType": "Argument"})
        _prop(arg, "stringProp", "Argument.name", var)
        _prop(arg, "stringProp", "Argument.value", "")
        _prop(arg, "stringProp", "Argument.metadata", "=")
    return root, plan_tree


def serialize(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="unicode")
    # Control characters (from recorded binary bodies) cannot appear raw in XML; write them as
    # character references, the way JMeter itself saves them.
    xml = _CONTROL_CHARS.sub(lambda m: f"&#x{ord(m.group()):x};", xml)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n"


def _common_headers(requests: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Headers every request sends with the same value (they go in one plan-level Header Manager)."""
    if not requests:
        return []
    rest = [{(n.lower(), v) for n, v in r["headers"]} for r in requests[1:]]
    return [(n, v) for n, v in requests[0]["headers"] if all((n.lower(), v) in other for other in rest)]


def _url_port(url: SplitResult) -> int:
    try:
        port = url.port
    except ValueError:
        port = None
    return port or (443 if url.scheme == "https" else 80)


# Fields of the View Results Tree's save configuration, as in JMeter's recording template.
_VIEW_RESULTS_FIELDS = [
    ("time", "true"), ("latency", "true"), ("timestamp", "true"), ("success", "true"), ("label", "true"),
    ("code", "true"), ("message", "true"), ("threadName", "true"), ("dataType", "true"), ("encoding", "false"),
    ("assertions", "true"), ("subresults", "true"), ("responseData", "false"), ("samplerData", "false"),
    ("xml", "false"), ("fieldNames", "true"), ("responseHeaders", "false"), ("requestHeaders", "false"),
    ("responseDataOnError", "false"), ("saveAssertionResultsFailureMessage", "true"),
    ("assertionsResultsToSave", "0"), ("bytes", "true"), ("url", "true"), ("hostname", "true"),
    ("threadCounts", "true"), ("sampleCount", "true"), ("idleTime", "true"), ("connectTime", "true"),
]


def build_recorded_plan(
    name: str,
    transactions: list[tuple[str, list[dict[str, Any]]]],
    *,
    variables: Iterable[str] = (),
    users: int = 1,
    ramp_up_seconds: int = 0,
    loops: int = 1,
    timeout_ms: int = 30_000,
) -> str:
    """Plan for an imported recording: one Transaction Controller per business function.

    Test Plan
      HTTP Request Defaults, HTTP Cookie Manager, common HTTP Header Manager   (config elements)
      Thread Group
        Transaction Controller "01 <business function>"
          HTTP Request (redirects are replayed as recorded, not followed)
            HTTP Header Manager                                               (config element)
            Response Assertion: recorded status code                          (assertion)
      View Results Tree, disabled so it costs nothing under load              (listener)
    """
    root, plan_tree = _test_plan(name, variables)

    defaults, _ = _element(plan_tree, "ConfigTestElement", "HttpDefaultsGui", "ConfigTestElement", "HTTP Request Defaults")
    dargs = ET.SubElement(defaults, "elementProp", {
        "name": "HTTPsampler.Arguments", "elementType": "Arguments", "guiclass": "HTTPArgumentsPanel",
        "testclass": "Arguments", "testname": "User Defined Variables", "enabled": "true",
    })
    ET.SubElement(dargs, "collectionProp", {"name": "Arguments.arguments"})
    _prop(defaults, "stringProp", "HTTPSampler.connect_timeout", timeout_ms)
    _prop(defaults, "stringProp", "HTTPSampler.response_timeout", timeout_ms)

    cookies, _ = _element(plan_tree, "CookieManager", "CookiePanel", "CookieManager", "HTTP Cookie Manager")
    ET.SubElement(cookies, "collectionProp", {"name": "CookieManager.cookies"})
    _prop(cookies, "boolProp", "CookieManager.clearEachIteration", True)   # each iteration is a new visitor

    common = _common_headers([r for _, requests in transactions for r in requests])
    if common:
        _add_header_manager(plan_tree, common, "Common headers")
    common_keys = {(n.lower(), v) for n, v in common}

    tg, tg_tree = _element(plan_tree, "ThreadGroup", "ThreadGroupGui", "ThreadGroup", "Recorded flow")
    _prop(tg, "stringProp", "ThreadGroup.on_sample_error", "continue")
    loop = ET.SubElement(tg, "elementProp", {
        "name": "ThreadGroup.main_controller", "elementType": "LoopController",
        "guiclass": "LoopControlPanel", "testclass": "LoopController",
        "testname": "Loop Controller", "enabled": "true",
    })
    _prop(loop, "boolProp", "LoopController.continue_forever", False)
    _prop(loop, "stringProp", "LoopController.loops", loops)
    _prop(tg, "stringProp", "ThreadGroup.num_threads", users)
    _prop(tg, "stringProp", "ThreadGroup.ramp_time", ramp_up_seconds)
    _prop(tg, "boolProp", "ThreadGroup.scheduler", False)
    _prop(tg, "stringProp", "ThreadGroup.duration", "")
    _prop(tg, "stringProp", "ThreadGroup.delay", "")
    _prop(tg, "boolProp", "ThreadGroup.same_user_on_next_iteration", True)

    for number, (tx_name, requests) in enumerate(transactions, 1):
        tc_tree = add_transaction_controller(tg_tree, f"{number:02d} {tx_name}")
        for r in requests:
            url = urlsplit(r["url"])
            sampler_tree = _add_http_sampler(
                tc_tree, r["label"], url_parts=url, port=_url_port(url), method=r["method"],
                body=r["body"], follow_redirects=False,
                content_encoding="UTF-8" if r["body"] is not None else None,
            )
            own = [(n, v) for n, v in r["headers"] if (n.lower(), v) not in common_keys]
            if own:
                _add_header_manager(sampler_tree, own)
            if r.get("status"):
                _add_status_assertion(sampler_tree, [r["status"]], ignore_status=True)

    vrt, _ = _element(plan_tree, "ResultCollector", "ViewResultsFullVisualizer", "ResultCollector", "View Results Tree")
    vrt.set("enabled", "false")
    _prop(vrt, "boolProp", "ResultCollector.error_logging", False)
    obj = ET.SubElement(vrt, "objProp")
    ET.SubElement(obj, "name").text = "saveConfig"
    value = ET.SubElement(obj, "value", {"class": "SampleSaveConfiguration"})
    for field, flag in _VIEW_RESULTS_FIELDS:
        ET.SubElement(value, field).text = flag
    _prop(vrt, "stringProp", "filename", "")

    return serialize(root)


def jmeter_properties() -> list[str]:
    """-J properties so the results file has the columns the analyzer reads."""
    return [
        "-Jjmeter.save.saveservice.output_format=csv",
        "-Jjmeter.save.saveservice.timestamp_format=ms",
        "-Jjmeter.save.saveservice.thread_counts=true",
        "-Jjmeter.save.saveservice.response_data.on_error=false",
        "-Jjmeter.save.saveservice.assertion_results_failure_message=true",
    ]
