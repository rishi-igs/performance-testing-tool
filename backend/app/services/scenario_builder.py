"""Combine the scripts of a scenario into one JMeter plan (LoadRunner's Controller).

Each group's script is built as for an export, then moved into the group's own Thread Groups:
its config elements (defaults, cookie and header managers, data sets) go inside each Thread
Group, so scripts do not leak settings into each other. Only standard JMeter elements are used:

  schedule      Thread Group: start delay, ramp-up, duration or iterations. A ramp-down splits
                the group into up to 10 cohorts that stop one after another.
  rendezvous    Synchronizing Timer on the first request of the business function
  goal          Precise Throughput Timer: on every request (hits per second), or on a Flow
                Control Action at the start of each iteration (transactions per second)
  network speed httpclient.socket.http(s).cps properties (bytes per second per connection)
"""
from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from ..models.scenario import Group, ScenarioConfig, Schedule
from ..models.script import FilterRules
from . import request_filter, xmlsafe
from .jmeter_plan_builder import _element, _prop, _test_plan, add_variable, serialize
from .script_builder import _pause, _regular_thread_groups, build_tree

MAX_COHORTS = 10
LISTENERS = {"ResultCollector", "BackendListener", "Summariser"}
_NUMBER = re.compile(r"^\d{2,} ")


class ScenarioError(ValueError):
    """The scenario cannot be built; the message is shown to the user."""


@dataclass
class Cohort:
    users: int
    delay: int          # seconds after the test starts
    ramp: int
    duration: int | None  # seconds after its own start; None = run its iterations
    loops: int | None
    label: str


@dataclass
class ScenarioPlan:
    xml: str
    files: dict[str, bytes]
    properties: list[str]
    hosts: list[str]
    groups: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    duration_seconds: int = 0
    total_users: int = 0


def cohorts(users: int, schedule: Schedule, name: str) -> list[Cohort]:
    """Thread Groups that together follow the schedule (users start and stop evenly)."""
    delay, ramp = schedule.start_delay_seconds, schedule.ramp_up_seconds
    if schedule.iterations:
        return [Cohort(users, delay, ramp, None, schedule.iterations, name)]
    hold_end = ramp + schedule.duration_seconds
    if not schedule.ramp_down_seconds or users == 1:
        return [Cohort(users, delay, ramp, hold_end + schedule.ramp_down_seconds, None, name)]
    k = min(users, MAX_COHORTS)
    sizes = [users // k + (1 if i < users % k else 0) for i in range(k)]
    out, started = [], 0
    for i, size in enumerate(sizes):
        start = ramp * started / users
        stop = hold_end + schedule.ramp_down_seconds * (i + 1) / k
        out.append(Cohort(size, delay + round(start), round(ramp * size / users), round(stop - start), None,
                          f"{name} ({i + 1}/{k})"))
        started += size
    return out


def planned_users(cfg: ScenarioConfig, step: int | None = None) -> dict[str, Any]:
    """The schedule as running users over time, per group (LoadRunner's scenario schedule graph)."""
    total = cfg.total_seconds()
    step = step or max(1, total // 200)
    series = {}
    for group, users in _group_users(cfg):
        events: list[tuple[float, int]] = []
        for c in cohorts(users, cfg.schedule_for(group), group.name):
            for j in range(c.users):
                events.append((c.delay + (c.ramp * j / c.users if c.ramp else 0), 1))
            if c.duration is not None:
                events.append((c.delay + c.duration, -c.users))
        points = []
        for t in range(0, total + step, step):
            points.append({"t": t, "users": sum(d for at, d in events if at <= t)})
        series[group.name] = points
    return {"step_seconds": step, "total_seconds": total, "groups": series,
            "iterations": any(cfg.schedule_for(g).iterations for g in cfg.active_groups())}


def _group_users(cfg: ScenarioConfig) -> list[tuple[Group, int]]:
    groups = cfg.active_groups()
    if cfg.mode != "goal":
        return [(g, g.users) for g in groups]
    weight = sum(g.users for g in groups)
    shares = [max(1, round(cfg.goal.max_users * g.users / weight)) for g in groups]
    return list(zip(groups, shares))


def _pairs(tree: ET.Element):
    children = list(tree)
    i = 0
    while i < len(children):
        el = children[i]
        sub = children[i + 1] if i + 1 < len(children) and children[i + 1].tag == "hashTree" else ET.Element("hashTree")
        if el.tag != "hashTree":
            yield el, sub
        i += 2 if i + 1 < len(children) and children[i + 1].tag == "hashTree" else 1


def _prop_value(el: ET.Element, name: str, default: str = "") -> str:
    found = next((c for c in el if c.get("name") == name), None)
    return (found.text or default) if found is not None else default


def _strip_listeners(tree: ET.Element) -> None:
    """Results go to the run's own results file; listeners only cost time under load."""
    children = list(tree)
    for i, el in enumerate(children):
        if el.tag in LISTENERS:
            tree.remove(el)
            if i + 1 < len(children) and children[i + 1].tag == "hashTree":
                tree.remove(children[i + 1])
    for el in tree:
        if el.tag == "hashTree":
            _strip_listeners(el)


def _external_files(tree: ET.Element) -> list[str]:
    return [p.text for el in tree.iter("CSVDataSet") for p in el
            if p.get("name") == "filename" and p.text and not p.text.startswith("data/")]


def _thread_group(parent: ET.Element, cohort: Cohort, users: int, *, on_error: str, same_user: bool) -> ET.Element:
    tg, tg_tree = _element(parent, "ThreadGroup", "ThreadGroupGui", "ThreadGroup", cohort.label)
    _prop(tg, "stringProp", "ThreadGroup.on_sample_error", on_error)
    loop = ET.SubElement(tg, "elementProp", {
        "name": "ThreadGroup.main_controller", "elementType": "LoopController", "guiclass": "LoopControlPanel",
        "testclass": "LoopController", "testname": "Loop Controller", "enabled": "true"})
    _prop(loop, "boolProp", "LoopController.continue_forever", False)
    _prop(loop, "intProp", "LoopController.loops", cohort.loops or -1)
    _prop(tg, "stringProp", "ThreadGroup.num_threads", users)
    _prop(tg, "stringProp", "ThreadGroup.ramp_time", cohort.ramp)
    _prop(tg, "boolProp", "ThreadGroup.scheduler", cohort.duration is not None or cohort.delay > 0)
    # Iterations with a start delay still need the scheduler; give it a duration that never ends first.
    _prop(tg, "stringProp", "ThreadGroup.duration", cohort.duration if cohort.duration is not None else 7 * 86_400)
    _prop(tg, "stringProp", "ThreadGroup.delay", cohort.delay)
    _prop(tg, "boolProp", "ThreadGroup.same_user_on_next_iteration", same_user)
    return tg_tree


def _sync_timer(name: str, size: int, timeout_s: int) -> ET.Element:
    el = ET.Element("SyncTimer", {"guiclass": "TestBeanGUI", "testclass": "SyncTimer",
                                  "testname": f"Rendezvous: {name}", "enabled": "true"})
    _prop(el, "intProp", "groupSize", size)
    _prop(el, "longProp", "timeoutInMs", timeout_s * 1000)
    return el


def _throughput_timer(label: str, per_second: float, duration: int) -> ET.Element:
    el = ET.Element("PreciseThroughputTimer", {"guiclass": "TestBeanGUI", "testclass": "PreciseThroughputTimer",
                                               "testname": label, "enabled": "true"})
    for name, value in (("allowedThroughputSurplus", 1.0), ("exactLimit", 10000), ("throughput", round(per_second, 6)),
                        ("throughputPeriod", 1), ("duration", max(1, duration)), ("batchSize", 1),
                        ("batchThreadDelay", 0), ("randomSeed", 0)):
        _prop(el, "stringProp", name, value)
    return el


def _first_sampler(tree: ET.Element) -> ET.Element | None:
    """The hashTree of the first HTTP request inside a Transaction Controller's tree."""
    for el, sub in _pairs(tree):
        if el.tag in ("HTTPSamplerProxy", "HTTPSampler"):
            return sub
        if el.tag.endswith("Controller"):
            found = _first_sampler(sub)
            if found is not None:
                return found
    return None


def build_scenario(cfg: ScenarioConfig, scripts: dict[str, dict[str, Any]],
                   files_by_script: dict[str, dict[str, bytes]], *, engines: int = 1) -> ScenarioPlan:
    """The plan for a scenario. `engines` > 1 divides users across distributed load generators."""
    root, plan, plan_tree = _test_plan(cfg.name)
    files: dict[str, bytes] = {}
    hosts: set[str] = set()
    warnings: list[str] = []
    groups_info = []
    udv: dict[str, str] = {}
    total_seconds = cfg.total_seconds()

    for group, users in _group_users(cfg):
        script = scripts.get(group.script_id)
        if script is None:
            raise ScenarioError(f"Group {group.name}: script {group.script_id} no longer exists")
        rules = FilterRules(**script["rules"])
        view = request_filter.evaluate(script["source"], script["items"], rules, script["overrides"])
        if not view["transactions"]:
            raise ScenarioError(f"Group {group.name}: the script has no kept requests")
        tree, built = build_tree(script, view, data=files_by_script.get(group.script_id, {}),
                                 settings_override=group.settings)
        hosts |= set(built.hosts)
        warnings += [f"{group.name}: {w}" for w in built.warnings]
        missing = sorted(built.unresolved) + built.empty
        if missing:
            warnings.append(f"{group.name}: these variables have no value: " + ", ".join(f"${{{v}}}" for v in missing))
        for path, content in built.files.items():
            files[f"data/{group.script_id}/{path.split('/', 1)[1]}"] = content

        coll = tree.plan.find("elementProp[@name='TestPlan.user_defined_variables']/collectionProp")
        for arg in coll.findall("elementProp") if coll is not None else []:
            name, value = arg.get("name"), _prop_value(arg, "Argument.value")
            if name in udv and udv[name] != value:
                warnings.append(f"Variable {name} has different values in two scripts; the first one is used.")
            udv.setdefault(name, value)

        shared = []                                     # the script's plan-level elements
        for el, sub in _pairs(tree.plan_tree):
            if el.tag.endswith("ThreadGroup") or el.tag in LISTENERS:
                continue
            if el.tag == "CSVDataSet":
                for prop in el:
                    if prop.get("name") == "filename" and (prop.text or "").startswith("data/"):
                        # Distributed engines read data files from their own disk: the folder comes
                        # from the perf.data_dir property (GENERATOR_DATA_DIR) sent to every engine.
                        prefix = "${__P(perf.data_dir,.)}/" if engines > 1 else ""
                        prop.text = f"{prefix}data/{group.script_id}/{prop.text[5:]}"
            shared.append((el, sub))
        for el, sub in _pairs(tree.plan_tree):
            if el.tag in ("SetupThreadGroup", "PostThreadGroup") and el.get("enabled", "true") != "false":
                el.set("testname", f"{group.name} · {el.get('testname')}")
                plan_tree.extend([copy.deepcopy(el), copy.deepcopy(sub)])

        script_groups = _regular_thread_groups(tree)
        for name in sorted(set(_external_files(tree.root))):
            warnings.append(f"{group.name}: the plan reads {name}, which must exist on the machine that runs JMeter.")
        transactions = [t["name"] for t in view["transactions"]]
        per_iteration = max(1, len(transactions))
        made = []
        schedule = cfg.schedule_for(group)
        group_cohorts = cohorts(users, schedule, group.name)
        for tg_el, tg_tree in script_groups:
            on_error = _prop_value(tg_el, "ThreadGroup.on_sample_error", "continue")
            same_user = _prop_value(tg_el, "ThreadGroup.same_user_on_next_iteration", "false") == "true"
            for cohort in group_cohorts:
                if len(script_groups) > 1:
                    cohort = Cohort(**{**cohort.__dict__, "label": f"{cohort.label} · {tg_el.get('testname')}"})
                per_engine = max(1, round(cohort.users / engines))
                new_tree = _thread_group(plan_tree, cohort, per_engine, on_error=on_error, same_user=same_user)
                for el, sub in shared:
                    new_tree.extend([copy.deepcopy(el), copy.deepcopy(sub)])
                new_tree.extend(copy.deepcopy(child) for child in tg_tree)
                _strip_listeners(new_tree)
                for r in cfg.rendezvous:
                    if r.group.lower() != group.name.lower():
                        continue
                    target = None
                    for el, sub in _pairs(new_tree):
                        if el.tag == "TransactionController" and _NUMBER.sub("", el.get("testname", "")).lower() == r.transaction.lower():
                            target = _first_sampler(sub)
                            break
                    if target is None:
                        warnings.append(f"Rendezvous {r.name}: {group.name} has no business function {r.transaction}")
                        continue
                    target.insert(0, ET.Element("hashTree"))
                    target.insert(0, _sync_timer(r.name, max(1, round(per_engine * r.percent / 100)), r.timeout_seconds))
                if cfg.mode == "goal":
                    share = users / max(1, sum(u for _, u in _group_users(cfg)))
                    per_second = cfg.goal.target * share / engines / len(group_cohorts) / len(script_groups)
                    if cfg.goal.type == "hits_per_second":
                        new_tree.insert(0, ET.Element("hashTree"))
                        new_tree.insert(0, _throughput_timer(f"Goal: {per_second:g} requests/s", per_second, total_seconds))
                    else:
                        action, sub = _pause("Goal pacing", _throughput_timer(
                            f"Goal: {per_second / per_iteration:g} iterations/s", per_second / per_iteration, total_seconds))
                        new_tree.insert(0, sub)
                        new_tree.insert(0, action)
                made.append({"name": cohort.label, "users": per_engine * engines})
        groups_info.append({"name": group.name, "script_id": group.script_id, "script": script["name"],
                            "users": users, "thread_groups": made, "transactions": transactions,
                            "schedule": schedule.model_dump()})

    for name, value in udv.items():
        add_variable(plan, name, value)
    if engines > 1 and files:
        warnings.append(f"Copy the data files (Download plan) to every load generator; their folder is read from "
                        f"GENERATOR_DATA_DIR. {len(files)} file(s): " + ", ".join(sorted(files)))
    properties = []
    if cfg.bandwidth_kbps:
        cps = cfg.bandwidth_kbps * 1000 // 8
        properties = [f"-Jhttpclient.socket.http.cps={cps}", f"-Jhttpclient.socket.https.cps={cps}"]
    total_users = sum(tg["users"] for g in groups_info for tg in g["thread_groups"])
    return ScenarioPlan(xml=xmlsafe.restore(serialize(root)), files=files, properties=properties,
                        hosts=sorted(hosts), groups=groups_info, warnings=warnings,
                        duration_seconds=total_seconds, total_users=total_users)
