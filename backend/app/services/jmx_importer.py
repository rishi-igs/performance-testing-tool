"""Import an existing JMeter .jmx and rebuild it with requests grouped by business function.

Export only changes the controller layout. Each request keeps its own children (header
managers, extractors, assertions, timers), and plan- and thread-group-level elements stay
where they were. Transaction, Simple and Recording Controllers are replaced by the new
grouping; elements scoped to one of them move with its requests. Other logic controllers
(If, Loop, While, ...) are kept as one block so their behaviour does not change.
"""
from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlsplit

from . import xmlsafe
from .har_parser import MAX_REQUESTS, ImportFailed
from .jmeter_plan_builder import PlanTree, add_transaction_controller, serialize

GROUPING = {"TransactionController", "GenericController", "RecordingController"}
_EXPORT_NUMBER = re.compile(r"^\d{2,} (?=\S)")     # "01 Login" as written by rebuild()/build_recorded_plan()
_visible = xmlsafe.visible

Pair = tuple[ET.Element, "ET.Element | None"]


def _is_thread_group(tag: str) -> bool:
    return tag.endswith("ThreadGroup")


def _is_sampler(tag: str) -> bool:
    return tag == "TestAction" or tag.endswith("Sampler") or tag.endswith("SamplerProxy")


def _is_logic_controller(tag: str) -> bool:
    return tag not in GROUPING and (tag.endswith("Controller") or tag in {"InterleaveControl", "RunTime"})


def _enabled(el: ET.Element) -> bool:
    return el.get("enabled", "true") != "false"


def _pairs(tree: ET.Element) -> Iterator[Pair]:
    """JMeter stores each element followed by a <hashTree> holding its children."""
    children = list(tree)
    i = 0
    while i < len(children):
        el = children[i]
        sub = children[i + 1] if i + 1 < len(children) and children[i + 1].tag == "hashTree" else None
        if el.tag != "hashTree":
            yield el, sub
        i += 2 if sub is not None else 1


def _prop(el: ET.Element, name: str) -> str:
    for child in el:
        if child.get("name") == name:
            return (child.text or "").strip()
    return ""


def _http_defaults(tree: ET.Element) -> dict[str, str]:
    for el, _ in _pairs(tree):
        if el.tag == "ConfigTestElement" and el.get("guiclass") == "HttpDefaultsGui" and _enabled(el):
            found = {k: _prop(el, f"HTTPSampler.{k}") for k in ("domain", "port", "protocol")}
            return {k: v for k, v in found.items() if v}
    return {}


@dataclass
class _Container:
    name: str
    tag: str
    scoped: list[Pair] = field(default_factory=list)


@dataclass
class _Item:
    el: ET.Element
    sub: ET.Element | None
    thread_group: int
    chain: list[int]        # enclosing grouping controllers, outermost first
    disabled: bool


@dataclass
class _Plan:
    root: ET.Element
    name: str
    plan_el: ET.Element
    plan_tree: ET.Element
    thread_groups: list[tuple[ET.Element, ET.Element]] = field(default_factory=list)
    tg_kept: list[list[Pair]] = field(default_factory=list)
    defaults: list[dict[str, str]] = field(default_factory=list)
    containers: list[_Container] = field(default_factory=list)
    items: list[_Item] = field(default_factory=list)


def _load(raw: bytes) -> _Plan:
    try:
        root = xmlsafe.parse(raw)
    except xmlsafe.UnsafeXml as exc:
        raise ImportFailed(str(exc).replace("The file", "The .jmx file", 1)) from exc
    except ET.ParseError as exc:
        raise ImportFailed(f"This is not a valid .jmx file: {exc}.") from exc
    if root.tag != "jmeterTestPlan":
        raise ImportFailed(f"This is not a JMeter test plan: the root element is <{root.tag}>.")
    top = root.find("hashTree")
    first = next(_pairs(top), None) if top is not None else None
    if first is None or first[0].tag != "TestPlan" or first[1] is None:
        raise ImportFailed("The .jmx file has no Test Plan.")
    plan_el, plan_tree = first
    plan = _Plan(root=root, name=plan_el.get("testname") or "Imported test plan", plan_el=plan_el, plan_tree=plan_tree)
    plan_defaults = _http_defaults(plan_tree)

    def walk(tree: ET.Element, tg: int, chain: list[int], disabled: bool) -> list[Pair]:
        kept: list[Pair] = []
        for el, sub in _pairs(tree):
            off = disabled or not _enabled(el)
            if el.tag in GROUPING:
                cid = len(plan.containers)
                plan.containers.append(_Container(el.get("testname") or el.tag, el.tag))
                if sub is not None:
                    plan.containers[cid].scoped = walk(sub, tg, chain + [cid], off)
            elif _is_sampler(el.tag) or _is_logic_controller(el.tag):
                plan.items.append(_Item(el, sub, tg, chain, off))
            else:
                kept.append((el, sub))
        return kept

    for el, sub in _pairs(plan_tree):
        if _is_thread_group(el.tag) and sub is not None:
            tg = len(plan.thread_groups)
            plan.thread_groups.append((el, sub))
            plan.defaults.append({**plan_defaults, **_http_defaults(sub)})
            plan.tg_kept.append(walk(sub, tg, [], not _enabled(el)))
    if len(plan.items) > MAX_REQUESTS:
        raise ImportFailed(f"The .jmx file has {len(plan.items)} samplers; the limit is {MAX_REQUESTS}.")
    return plan


def _sampler_url(el: ET.Element, defaults: dict[str, str]) -> str:
    path = _prop(el, "HTTPSampler.path")
    if re.match(r"^https?://", path, re.IGNORECASE):
        return path
    protocol = _prop(el, "HTTPSampler.protocol") or defaults.get("protocol") or "http"
    domain = _prop(el, "HTTPSampler.domain") or defaults.get("domain", "")
    port = _prop(el, "HTTPSampler.port") or defaults.get("port", "")
    if port in {"", {"http": "80", "https": "443"}.get(protocol.lower())}:
        port = ""
    if path and not path.startswith("/"):
        path = "/" + path
    return f"{protocol}://{domain}{':' + port if port else ''}{path or '/'}"


def _describe(plan: _Plan, item: _Item, index: int) -> dict[str, Any]:
    el = item.el
    name = _visible(el.get("testname") or el.tag)
    group = next((cid for cid in reversed(item.chain) if plan.containers[cid].tag != "RecordingController"), None)
    described: dict[str, Any] = {
        "index": index, "kind": "other", "label": name, "method": None, "url": None, "host": None,
        "status": None, "mime": None, "resource_type": None, "size": None, "time_ms": None,
        "started_ms": None, "page": f"c{group}" if group is not None else None,
        "page_name": _visible(_EXPORT_NUMBER.sub("", plan.containers[group].name)) if group is not None else None,
        "error": None, "disabled": item.disabled, "thread_group": item.thread_group, "note": None,
    }
    if el.tag in ("HTTPSamplerProxy", "HTTPSampler"):
        url = _visible(_sampler_url(el, plan.defaults[item.thread_group]))
        described.update(kind="http", method=(_prop(el, "HTTPSampler.method") or "GET").upper(), url=url,
                         host=(urlsplit(url).hostname or "").lower())
    elif _is_logic_controller(el.tag):
        described.update(label=f"{name} ({el.tag})", note="Logic controller: kept as one block with everything inside it")
    return described


def parse_jmx(raw: bytes) -> dict[str, Any]:
    plan = _load(raw)
    if not plan.thread_groups:
        raise ImportFailed("The .jmx file has no Thread Group.")
    if not plan.items:
        raise ImportFailed("The .jmx file has no samplers to import.")
    items = [_describe(plan, item, i) for i, item in enumerate(plan.items)]

    warnings = ["A .jmx file holds the requests but not what the server answered, so dynamic values cannot be "
                "traced at import. On the Correlation tab, Correlate automatically replays the script and finds them."]
    blocks = sum(1 for i in items if i["note"])
    if blocks:
        warnings.append(f"{blocks} logic controller(s) (If, Loop, While, ...) are kept as single blocks; "
                        "requests inside them cannot be filtered one by one.")
    disabled = sum(1 for i in items if i["disabled"])
    if disabled:
        warnings.append(f"{disabled} sampler(s) were disabled in the imported plan and start out excluded.")
    if any(c.scoped for c in plan.containers):
        warnings.append("Elements placed inside controllers (for example timers or header managers) move "
                        "with that controller's requests into the new Transaction Controllers.")
    return {"name": plan.name, "items": items, "warnings": warnings, "variables": [],
            "design": {}, "suggestions": {}}


def rebuild(raw: bytes, transactions: list[dict[str, Any]]) -> str:
    """Re-create the plan with one Transaction Controller per transaction (see rebuild_tree)."""
    return xmlsafe.restore(serialize(rebuild_tree(raw, transactions).root))


def rebuild_tree(raw: bytes, transactions: list[dict[str, Any]]) -> PlanTree:
    """Re-create the plan with one Transaction Controller per transaction.

    `transactions`: [{"thread_group", "number", "name", "items": [item index, ...]}] in run order.
    Items not listed are left out. Text keeps xmlsafe markers; pass the serialized XML through
    xmlsafe.restore().
    """
    plan = _load(raw)
    tree = PlanTree(root=plan.root, plan=plan.plan_el, plan_tree=plan.plan_tree,
                    thread_groups=list(plan.thread_groups))
    by_group: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for tx in transactions:
        by_group[tx["thread_group"]].append(tx)

    for tg, (_, tg_tree) in enumerate(plan.thread_groups):
        for child in list(tg_tree):
            tg_tree.remove(child)
        for el, sub in plan.tg_kept[tg]:
            tg_tree.extend([el, sub if sub is not None else ET.Element("hashTree")])
        for tx in by_group.get(tg, []):
            tc_tree = add_transaction_controller(tg_tree, f"{tx['number']:02d} {tx['name']}")
            copied: set[int] = set()
            for index in tx["items"]:
                item = plan.items[index]
                for cid in item.chain:
                    if cid not in copied:
                        copied.add(cid)
                        for el, sub in plan.containers[cid].scoped:
                            tc_tree.extend([copy.deepcopy(el), copy.deepcopy(sub) if sub is not None else ET.Element("hashTree")])
                item.el.set("enabled", "true")    # the user chose to keep it
                sub = item.sub if item.sub is not None else ET.Element("hashTree")
                tc_tree.extend([item.el, sub])
                if item.el.tag in ("HTTPSamplerProxy", "HTTPSampler"):
                    tree.samplers[index] = (item.el, sub, tc_tree)
    return tree
