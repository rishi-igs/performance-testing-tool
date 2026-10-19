"""Turn an imported script and its design into a runnable JMeter plan.

The design is what LoadRunner's VuGen calls correlation, parameters, checks and run-time
settings. Each part becomes ordinary JMeter elements, so the exported .jmx opens and runs in
the JMeter GUI:

  correlation      JSON Extractor / Regular Expression Extractor / Boundary Extractor on the
                   request whose response holds the value
  parameters       CSV Data Set Config (files, sequential lists), Counter (unique numbers),
                   Random Variable (random numbers), or a JMeter function at each use
                   (__UUID, __time/__timeShift, __RandomString, __Random)
  checks           Response Assertion, JSON Assertion, Duration Assertion, Size Assertion
  think time       Flow Control Action with a Constant or Uniform Random Timer between
                   business functions (outside the Transaction Controllers, so it does not
                   count as response time)
  pacing           Flow Control Action with a Constant Throughput Timer (start every N s) or
                   with a timer at the end of the iteration (wait N s after it)
  error handling   the Thread Group's "action after a sampler error"
"""
from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from ..models.script import Check, CorrelationRule, Parameter, RuntimeSettings, ScriptDesign
from . import xmlsafe
from .correlation import replace_value
from .jmeter_plan_builder import PlanTree, _element, _prop, add_variable, recorded_tree, serialize
from .jmx_importer import rebuild_tree

DATA_DIR = "data"                     # CSV files sit in this folder next to the plan
NOT_FOUND = "_NOT_FOUND"              # extractor default: ${token} becomes token_NOT_FOUND
TIMER_TAGS = {"ConstantTimer", "UniformRandomTimer", "GaussianRandomTimer", "PoissonRandomTimer"}
SCRIPTED_TAGS = ("JSR223", "BeanShell", "BSF")
_VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ON_ERROR = {"continue": "continue", "next_iteration": "startnextloop", "stop_user": "stopthread"}
_SHARE = {"all": "shareMode.all", "group": "shareMode.group", "user": "shareMode.thread"}
_RANDOM_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


@dataclass
class Built:
    xml: str
    files: dict[str, bytes]                                  # relative path -> content
    unresolved: dict[str, list[int]] = field(default_factory=dict)   # variable -> requests using it
    used: dict[str, list[int]] = field(default_factory=dict)         # every variable -> requests using it
    empty: list[str] = field(default_factory=list)           # used variables that are defined but empty
    scripted: bool = False                                   # JSR223/BeanShell may define more variables
    warnings: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)


# ---- small element helpers ------------------------------------------------------------------

def _regular_thread_groups(tree: PlanTree) -> list[tuple[ET.Element, ET.Element]]:
    return [(tg, sub) for tg, sub in tree.thread_groups if tg.tag not in ("SetupThreadGroup", "PostThreadGroup")]


def _set_prop(el: ET.Element, tag: str, name: str, value: object) -> None:
    existing = next((c for c in el if c.get("name") == name), None)
    if existing is None:
        _prop(el, tag, name, value)
    else:
        existing.text = str(value).lower() if isinstance(value, bool) else str(value)


def _insert(parent_tree: ET.Element, index: int, el: ET.Element) -> ET.Element:
    """Insert el and an empty hashTree at a position of parent_tree; return the hashTree."""
    sub = ET.Element("hashTree")
    parent_tree.insert(index, el)
    parent_tree.insert(index + 1, sub)
    return sub


def _new(tag: str, guiclass: str, testname: str, testclass: str | None = None) -> ET.Element:
    return ET.Element(tag, {"guiclass": guiclass, "testclass": testclass or tag, "testname": testname, "enabled": "true"})


def _java_hash(value: str) -> str:
    """Java's String.hashCode, which JMeter uses to name assertion strings (deterministic output)."""
    h = 0
    for ch in value:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    return str(h - 0x100000000 if h >= 0x80000000 else h)


def _escape_arg(value: str) -> str:
    """JMeter function arguments are comma-separated; a literal comma needs a backslash."""
    return value.replace("\\", "\\\\").replace(",", "\\,")


def _pause(name: str, timer: ET.Element) -> tuple[ET.Element, ET.Element]:
    """Flow Control Action (pause 0) whose child timer does the waiting, as in JMeter's templates."""
    action = _new("TestAction", "TestActionGui", name)
    _prop(action, "intProp", "ActionProcessor.action", 1)       # pause
    _prop(action, "intProp", "ActionProcessor.target", 0)       # current thread
    _prop(action, "stringProp", "ActionProcessor.duration", 0)
    sub = ET.Element("hashTree")
    sub.extend([timer, ET.Element("hashTree")])
    return action, sub


def _constant_timer(ms: float, name: str) -> ET.Element:
    timer = _new("ConstantTimer", "ConstantTimerGui", name)
    _prop(timer, "stringProp", "ConstantTimer.delay", int(ms))
    return timer


def _random_timer(min_ms: float, max_ms: float, name: str) -> ET.Element:
    timer = _new("UniformRandomTimer", "UniformRandomTimerGui", name)
    _prop(timer, "stringProp", "ConstantTimer.delay", int(min_ms))
    _prop(timer, "stringProp", "RandomTimer.range", int(max_ms - min_ms))
    return timer


# ---- text replacement -----------------------------------------------------------------------

def _texts(sampler: ET.Element, sub: ET.Element) -> list[ET.Element]:
    """Elements whose text is request content: path, body/arguments and header values."""
    out = [c for c in sampler if c.get("name") == "HTTPSampler.path"]
    out += [e for e in sampler.iter("stringProp") if e.get("name") == "Argument.value"]
    for manager in (c for c in sub if c.tag == "HeaderManager"):
        out += [e for e in manager.iter("stringProp") if e.get("name") == "Header.value"]
    return out


def _shared_header_values(tree: PlanTree) -> list[ET.Element]:
    """Header values of Header Managers outside any request (they apply to many requests)."""
    scopes = [tree.plan_tree] + [sub for _, sub in tree.thread_groups]
    return [e for scope in scopes for m in scope if m.tag == "HeaderManager"
            for e in m.iter("stringProp") if e.get("name") == "Header.value"]


def _function(p: Parameter) -> str | None:
    """JMeter expression for a parameter evaluated at each use, or None if it is a config element."""
    if p.type == "uuid":
        return "${__UUID()}"
    if p.type == "random_string":
        return f"${{__RandomString({p.length},{_RANDOM_CHARS},)}}"
    if p.type == "date":
        if p.format in ("epoch_ms", "epoch_s"):          # Unix time, as browsers send timestamps
            return "${__time(,)}" if p.format == "epoch_ms" else "${__time(/1000,)}"
        fmt = _escape_arg(p.format)
        return f"${{__time({fmt},)}}" if not p.offset_days else f"${{__timeShift({fmt},,P{p.offset_days}D,,)}}"
    if p.type == "random_number" and p.update == "occurrence":
        return f"${{__Random({p.minimum},{p.maximum},)}}"
    if p.type == "list" and p.selection == "random":
        return f"${{__V({p.name}__${{{p.name}__index}})}}"
    return None


def _replace_texts(tree: PlanTree, design: ScriptDesign) -> None:
    replacements = [(r.find, r.variable, None) for r in design.replacements]
    replacements += [(c.replace, c.variable, c.source) for c in design.correlations if c.replace]
    functions = {f"${{{p.name}}}": expr for p in design.parameters if (expr := _function(p))}

    def rewrite(el: ET.Element, index: int | None) -> None:
        text = el.text or ""
        for find, variable, after in replacements:
            if after is None or (index is not None and index > after):
                text, _ = replace_value(text, find, variable)
        for ref, expr in functions.items():
            text = text.replace(ref, expr)
        el.text = text

    for index, (sampler, sub, _) in tree.samplers.items():
        for el in _texts(sampler, sub):
            rewrite(el, index)
    for el in _shared_header_values(tree):
        rewrite(el, None)


# ---- design parts ---------------------------------------------------------------------------

def _extractor(rule: CorrelationRule) -> ET.Element:
    default = f"{rule.variable}{NOT_FOUND}"
    headers = "true" if rule.scope == "headers" else "false"
    if rule.extractor == "json":
        el = _new("JSONPostProcessor", "JSONPostProcessorGui", f"Correlation: {rule.variable}")
        _prop(el, "stringProp", "JSONPostProcessor.referenceNames", rule.variable)
        _prop(el, "stringProp", "JSONPostProcessor.jsonPathExprs", rule.expression)
        _prop(el, "stringProp", "JSONPostProcessor.match_numbers", rule.match)
        _prop(el, "stringProp", "JSONPostProcessor.defaultValues", default)
    elif rule.extractor == "regex":
        el = _new("RegexExtractor", "RegexExtractorGui", f"Correlation: {rule.variable}")
        _prop(el, "stringProp", "RegexExtractor.useHeaders", headers)
        _prop(el, "stringProp", "RegexExtractor.refname", rule.variable)
        _prop(el, "stringProp", "RegexExtractor.regex", rule.expression)
        _prop(el, "stringProp", "RegexExtractor.template", "$1$")
        _prop(el, "stringProp", "RegexExtractor.default", default)
        _prop(el, "boolProp", "RegexExtractor.default_empty_value", False)
        _prop(el, "stringProp", "RegexExtractor.match_number", rule.match)
    else:
        el = _new("BoundaryExtractor", "BoundaryExtractorGui", f"Correlation: {rule.variable}")
        _prop(el, "stringProp", "BoundaryExtractor.useHeaders", headers)
        _prop(el, "stringProp", "BoundaryExtractor.refname", rule.variable)
        _prop(el, "stringProp", "BoundaryExtractor.lboundary", rule.expression)
        _prop(el, "stringProp", "BoundaryExtractor.rboundary", rule.right or "")
        _prop(el, "stringProp", "BoundaryExtractor.default", default)
        _prop(el, "boolProp", "BoundaryExtractor.default_empty_value", False)
        _prop(el, "stringProp", "BoundaryExtractor.match_number", rule.match)
    return el


def _assertion(check: Check) -> ET.Element:
    if check.type in ("text", "not_text", "regex", "header"):
        el = _new("ResponseAssertion", "AssertionGui", f"Check: {check.type} {check.value}"[:120])
        strings = ET.SubElement(el, "collectionProp", {"name": "Asserion.test_strings"})   # sic
        _prop(strings, "stringProp", _java_hash(check.value or ""), check.value)
        label = {"text": "Text not found", "not_text": "Unwanted text found",
                 "regex": "Pattern not found", "header": "Header text not found"}[check.type]
        _prop(el, "stringProp", "Assertion.custom_message", f"{label}: {check.value}"[:500])
        field_name = "Assertion.response_headers" if check.type == "header" else "Assertion.response_data"
        _prop(el, "stringProp", "Assertion.test_field", field_name)
        _prop(el, "boolProp", "Assertion.assume_success", False)
        _prop(el, "intProp", "Assertion.test_type",
              {"text": 16, "not_text": 16 | 4, "regex": 2, "header": 16}[check.type])
    elif check.type == "json_path":
        el = _new("JSONPathAssertion", "JSONPathAssertionGui", f"Check: {check.path}"[:120])
        _prop(el, "stringProp", "JSON_PATH", check.path)
        _prop(el, "stringProp", "EXPECTED_VALUE", check.expected or "")
        _prop(el, "boolProp", "JSONVALIDATION", check.expected is not None)
        _prop(el, "boolProp", "EXPECT_NULL", False)
        _prop(el, "boolProp", "INVERT", False)
        _prop(el, "boolProp", "ISREGEX", False)
    elif check.type == "duration":
        el = _new("DurationAssertion", "DurationAssertionGui", f"Check: under {check.limit} ms")
        _prop(el, "stringProp", "DurationAssertion.duration", check.limit)
    else:
        el = _new("SizeAssertion", "SizeAssertionGui", f"Check: body at most {check.limit} bytes")
        _prop(el, "stringProp", "Assertion.test_field", "SizeAssertion.response_data")
        _prop(el, "stringProp", "SizeAssertion.size", check.limit)
        _prop(el, "intProp", "SizeAssertion.operator", 6)          # <=
    return el


def _csv_data_set(path: str, columns: list[str], *, header: bool, recycle: bool, stop: bool, share: str) -> ET.Element:
    el = _new("CSVDataSet", "TestBeanGUI", f"Data: {path.rsplit('/', 1)[-1]}")
    _prop(el, "stringProp", "filename", path)
    _prop(el, "stringProp", "fileEncoding", "UTF-8")
    _prop(el, "stringProp", "variableNames", ",".join(columns))
    _prop(el, "boolProp", "ignoreFirstLine", header)
    _prop(el, "stringProp", "delimiter", ",")
    _prop(el, "boolProp", "quotedData", True)
    _prop(el, "boolProp", "recycle", recycle)
    _prop(el, "boolProp", "stopThread", stop)
    _prop(el, "stringProp", "shareMode", share)
    return el


def _random_variable(name: str, minimum: int, maximum: int, label: str) -> ET.Element:
    el = _new("RandomVariableConfig", "TestBeanGUI", label)
    _prop(el, "stringProp", "variableName", name)
    _prop(el, "stringProp", "outputFormat", "")
    _prop(el, "stringProp", "minimumValue", minimum)
    _prop(el, "stringProp", "maximumValue", maximum)
    _prop(el, "stringProp", "randomSeed", "")
    _prop(el, "boolProp", "perThread", True)
    return el


def _config_elements(tree: PlanTree, design: ScriptDesign, data: dict[str, bytes]) -> dict[str, bytes]:
    """Data sets and generators at Test Plan level; returns the data files the plan reads."""
    files: dict[str, bytes] = {}
    elements: list[ET.Element] = []
    for f in design.data_files:
        path = f"{DATA_DIR}/{f.name}"
        files[path] = data.get(f.name, b"")
        elements.append(_csv_data_set(path, f.columns, header=f.first_line_header, recycle=f.recycle,
                                      stop=f.stop_at_end, share=_SHARE[f.sharing]))
    for p in design.parameters:
        if p.type == "list" and p.selection == "sequential":
            path = f"{DATA_DIR}/{p.name}.csv"
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            for value in p.values:
                writer.writerow([value])
            files[path] = buf.getvalue().encode("utf-8")
            elements.append(_csv_data_set(path, [p.name], header=False, recycle=True, stop=False, share="shareMode.all"))
        elif p.type == "list":
            for i, value in enumerate(p.values, 1):
                add_variable(tree.plan, f"{p.name}__{i}", value)
            elements.append(_random_variable(f"{p.name}__index", 1, len(p.values), f"Random pick: {p.name}"))
        elif p.type == "random_number" and p.update == "iteration":
            elements.append(_random_variable(p.name, p.minimum, p.maximum, f"Random number: {p.name}"))
        elif p.type == "unique_number":
            el = _new("CounterConfig", "CounterConfigGui", f"Unique number: {p.name}")
            _prop(el, "stringProp", "CounterConfig.start", p.start)
            _prop(el, "stringProp", "CounterConfig.end", "")
            _prop(el, "stringProp", "CounterConfig.incr", p.increment)
            _prop(el, "stringProp", "CounterConfig.name", p.name)
            _prop(el, "stringProp", "CounterConfig.format", p.format)
            _prop(el, "boolProp", "CounterConfig.per_user", p.per_user)
            _prop(el, "boolProp", "CounterConfig.reset_on_tg_iteration", False)
            elements.append(el)
    for offset, el in enumerate(elements):
        _insert(tree.plan_tree, 2 * offset, el)
    return files


def _thread_group_settings(tree: PlanTree, design: ScriptDesign, *, source: str, debug: bool) -> None:
    settings = design.settings
    think, pacing = settings.think_time, settings.pacing
    transaction_number = 0
    for tg, tg_tree in _regular_thread_groups(tree):
        _set_prop(tg, "stringProp", "ThreadGroup.on_sample_error", _ON_ERROR[settings.on_error])
        if source == "jmx":
            _set_prop(tg, "boolProp", "ThreadGroup.same_user_on_next_iteration", not settings.new_user_each_iteration)
            if debug or think.mode != "recorded":
                _remove_timers(tg_tree)
        if debug:
            continue
        controllers = [el for el in tg_tree if el.tag == "TransactionController"]
        for position, tc in enumerate(controllers):
            transaction_number += 1
            if position == 0 or think.mode == "ignore":
                continue
            if think.mode == "fixed" and think.seconds > 0:
                timer = _constant_timer(think.seconds * 1000, f"Think time {think.seconds:g} s")
            elif think.mode == "random" and think.maximum > 0:
                timer = _random_timer(think.minimum * 1000, think.maximum * 1000,
                                      f"Think time {think.minimum:g}-{think.maximum:g} s")
            elif think.mode == "recorded" and source == "har":
                pause = tree.pauses_ms[transaction_number - 1] if transaction_number - 1 < len(tree.pauses_ms) else None
                if not pause:
                    continue
                ms = min(pause * think.multiplier, think.cap_seconds * 1000)
                if ms < 50:
                    continue
                timer = _constant_timer(ms, f"Think time {ms / 1000:.1f} s (recorded)")
            else:
                continue
            action, sub = _pause("Think time", timer)
            at = list(tg_tree).index(tc)
            tg_tree.insert(at, action)
            tg_tree.insert(at + 1, sub)

        if pacing.mode == "interval":
            ctt = _new("ConstantThroughputTimer", "TestBeanGUI", f"Pacing: start every {pacing.seconds:g} s")
            _prop(ctt, "intProp", "calcMode", 0)                   # this thread only
            _prop(ctt, "stringProp", "throughput", round(60 / pacing.seconds, 6))   # per minute
            action, sub = _pause("Pacing", ctt)
            first = next((i for i, el in enumerate(tg_tree) if el.tag in ("TransactionController", "TestAction")
                          or el.tag.endswith("Sampler") or el.tag.endswith("SamplerProxy")), len(tg_tree))
            tg_tree.insert(first, action)
            tg_tree.insert(first + 1, sub)
        elif pacing.mode in ("after_fixed", "after_random"):
            timer = (_constant_timer(pacing.seconds * 1000, f"Pacing: wait {pacing.seconds:g} s")
                     if pacing.mode == "after_fixed" else
                     _random_timer(pacing.minimum * 1000, pacing.maximum * 1000,
                                   f"Pacing: wait {pacing.minimum:g}-{pacing.maximum:g} s"))
            action, sub = _pause("Pacing", timer)
            tg_tree.extend([action, sub])


def _remove_timers(tree: ET.Element) -> None:
    children = list(tree)
    for i, el in enumerate(children):
        if el.tag in TIMER_TAGS:
            tree.remove(el)
            if i + 1 < len(children) and children[i + 1].tag == "hashTree":
                tree.remove(children[i + 1])
    for el in tree:
        if el.tag == "hashTree":
            _remove_timers(el)


def _debug_mode(tree: PlanTree) -> None:
    """One user, one iteration, every request labelled with its index (VuGen's replay)."""
    for tg, _ in _regular_thread_groups(tree):
        _set_prop(tg, "stringProp", "ThreadGroup.num_threads", 1)
        _set_prop(tg, "stringProp", "ThreadGroup.ramp_time", 0)
        _set_prop(tg, "boolProp", "ThreadGroup.scheduler", False)
        _set_prop(tg, "stringProp", "ThreadGroup.duration", "")
        _set_prop(tg, "stringProp", "ThreadGroup.delay", "")
        loop = tg.find("elementProp[@name='ThreadGroup.main_controller']")
        if loop is not None:
            _set_prop(loop, "boolProp", "LoopController.continue_forever", False)
            for c in list(loop):
                if c.get("name") == "LoopController.loops":
                    loop.remove(c)
            _prop(loop, "stringProp", "LoopController.loops", 1)
    for index, (sampler, _, _) in tree.samplers.items():
        sampler.set("testname", f"#{index} {sampler.get('testname')}")


def _debug_variables(tree: PlanTree, after: int) -> None:
    """Debug Sampler after a request, recording the variables extractors just set."""
    sampler, sub, parent = tree.samplers[after]
    el = _new("DebugSampler", "TestBeanGUI", f"[variables after #{after}]")
    _prop(el, "boolProp", "displayJMeterProperties", False)
    _prop(el, "boolProp", "displayJMeterVariables", True)
    _prop(el, "boolProp", "displaySystemProperties", False)
    _insert(parent, list(parent).index(sub) + 1, el)


# ---- variables report -------------------------------------------------------------------

def _defined_variables(root: ET.Element) -> tuple[dict[str, str], bool]:
    """Variables some element defines (name -> value for plain User Defined Variables)."""
    defined: dict[str, str] = {}
    scripted = False
    for el in root.iter():
        if el.tag.startswith(SCRIPTED_TAGS) and el.get("enabled", "true") != "false":
            scripted = True
        name = el.get("name") or ""
        text = el.text or ""
        if el.get("elementType") == "Argument":
            value = el.find("stringProp[@name='Argument.value']")
            defined[name] = (value.text or "") if value is not None else ""
        elif name in ("variableNames",):
            defined.update({v.strip(): "?" for v in text.split(",") if v.strip()})
        elif name == "JSONPostProcessor.referenceNames":
            defined.update({v.strip(): "?" for v in text.split(";") if v.strip()})
        elif name in ("RegexExtractor.refname", "BoundaryExtractor.refname", "XPathExtractor.refname",
                      "XPath2Extractor.refname", "HtmlExtractor.refname", "CounterConfig.name", "variableName"):
            if text.strip():
                defined[text.strip()] = "?"
    return defined, scripted


def plan_variables(tree: PlanTree) -> set[str]:
    """Variables the plan's own elements define (an imported .jmx may have extractors or data sets)."""
    return set(_defined_variables(tree.root)[0])


def _report(tree: PlanTree, built: Built) -> None:
    defined, built.scripted = _defined_variables(tree.root)
    used: dict[str, list[int]] = {}
    for index, (sampler, sub, _) in sorted(tree.samplers.items()):
        for el in _texts(sampler, sub) + [c for c in sampler if c.get("name") == "HTTPSampler.domain"]:
            for var in _VAR_REF.findall(el.text or ""):
                if index not in used.setdefault(var, []):
                    used[var].append(index)
    for el in _shared_header_values(tree):
        for var in _VAR_REF.findall(el.text or ""):
            used.setdefault(var, [])
    built.used = used
    built.unresolved = {v: i for v, i in used.items() if v not in defined and not v.startswith("COOKIE_")}
    built.empty = sorted(v for v in used if defined.get(v) == "")


def _hosts(tree: PlanTree, items: list[dict[str, Any]], defined: dict[str, str]) -> list[str]:
    hosts: set[str] = set()
    for index in tree.samplers:
        host = items[index].get("host") or ""
        hosts.add(_VAR_REF.sub(lambda m: defined.get(m.group(1), m.group(0)), host).lower())
    return sorted(hosts)


# ---- entry point --------------------------------------------------------------------------------

def build(script: dict[str, Any], view: dict[str, Any], *, users: int = 1, ramp_up_seconds: int = 0,
          loops: int = 1, debug: bool = False, data: dict[str, bytes] | None = None,
          settings_override: RuntimeSettings | None = None) -> Built:
    """Build the plan for the requests and business functions in `view` (request_filter.evaluate)."""
    tree, built = build_tree(script, view, users=users, ramp_up_seconds=ramp_up_seconds, loops=loops,
                             debug=debug, data=data, settings_override=settings_override)
    xml = serialize(tree.root)
    built.xml = xmlsafe.restore(xml) if script["source"] == "jmx" else xml
    return built


def build_tree(script: dict[str, Any], view: dict[str, Any], *, users: int = 1, ramp_up_seconds: int = 0,
               loops: int = 1, debug: bool = False, data: dict[str, bytes] | None = None,
               settings_override: RuntimeSettings | None = None) -> tuple[PlanTree, Built]:
    """Like build(), but return the element tree (text still holds xmlsafe markers for JMX scripts)."""
    design = ScriptDesign(**(script.get("design") or {}))
    if settings_override is not None:
        design.settings = settings_override
    settings = design.settings
    defined_by_design = ({p.name for p in design.parameters} | {c for f in design.data_files for c in f.columns}
                         | {c.variable for c in design.correlations})
    if script["source"] == "har":
        groups = [(t["name"], [script["items"][i] for i in t["items"]]) for t in view["transactions"]]
        placeholders = [v for v in script.get("variables") or [] if v not in defined_by_design]
        tree = recorded_tree(script["name"], groups, variables=placeholders, users=users,
                             ramp_up_seconds=ramp_up_seconds, loops=loops,
                             timeout_ms=settings.timeout_seconds * 1000,
                             clear_cookies=settings.new_user_each_iteration,
                             status_checks=settings.status_checks)
    else:
        tree = rebuild_tree(script["original"], view["transactions"])
    if debug:
        _debug_mode(tree)

    built = Built(xml="", files={})
    _replace_texts(tree, design)
    for rule in design.correlations:
        if rule.source not in tree.samplers:
            built.warnings.append(f"Correlation ${{{rule.variable}}}: its source request #{rule.source} is not "
                                  "in the plan (excluded, or not an HTTP request).")
            continue
        tree.samplers[rule.source][1].append(_extractor(rule))
        tree.samplers[rule.source][1].append(ET.Element("hashTree"))
    if debug:
        for source in sorted({r.source for r in design.correlations if r.source in tree.samplers}):
            _debug_variables(tree, source)
    for check in design.checks:
        targets = [check.item] if check.item is not None else sorted(tree.samplers)
        for index in targets:
            if index in tree.samplers:
                tree.samplers[index][1].extend([_assertion(check), ET.Element("hashTree")])
            elif check.item is not None:
                built.warnings.append(f"Check {check.id}: request #{index} is not in the plan.")
    built.files = _config_elements(tree, design, data or {})
    for f in design.data_files:
        if not (data or {}).get(f.name):
            built.warnings.append(f"Data file {f.name} has no content; upload it again.")
    _thread_group_settings(tree, design, source=script["source"], debug=debug)

    _report(tree, built)
    defined, _ = _defined_variables(tree.root)
    built.hosts = _hosts(tree, script["items"], defined)
    return tree, built
