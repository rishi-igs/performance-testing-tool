"""Debug replay of a script: one user, one iteration, full request and response details.

Correlate automatically: replay, find where the script's dynamic values come from in what the
server answered (services/autocorrelate), add the rules, and replay again until nothing new
turns up. This is how a .jmx, which holds no recorded responses, gets its correlations.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..config import Settings
from ..db import now_iso
from ..deps import audit, ensure_project, tester
from ..deps import viewer as require_api_key
from ..models.script import ScriptDesign
from ..services import autocorrelate, debug_runner, script_builder
from ..services.security import TargetNotAllowed, check_target
from ..services.test_executor import Outcome, TooManyTests, finish_status, run_paths
from .scripts import evaluate, get_script_or_404

router = APIRouter(tags=["debug"])
log = logging.getLogger("perf.debug")
DEBUG_MAX_SECONDS = 300
AUTO_MAX_REPLAYS = 4


def check_hosts(settings: Settings, hosts: list[str], who: str) -> None:
    """Every host a script calls must pass the same target checks as a quick test."""
    for host in hosts:
        if not host or "${" in host:
            raise HTTPException(422, f"Host '{host or '(empty)'}' cannot be checked before the run: use a literal "
                                     "host or define it in User Defined Variables.")
        try:
            check_target(f"http://{host}/", settings)
        except TargetNotAllowed as exc:
            audit.warning("REJECTED target by=%s host=%s reason=%s", who, host, exc)
            raise HTTPException(422, f"{host}: {exc}") from exc


def _replay_plan(state: Any, script: dict[str, Any], who: str, *, keep_going: bool):
    _, view = evaluate(script)
    if not view["transactions"]:
        raise HTTPException(422, "Nothing to replay: every request is excluded.")
    override = None
    if keep_going:          # a scan needs every response, so carry on after a failed request
        override = ScriptDesign(**script["design"]).settings.model_copy(update={"on_error": "continue"})
    built = script_builder.build(script, view, debug=True, data=state.db.script_files(script["id"]),
                                 settings_override=override)
    check_hosts(state.settings, built.hosts, who)
    return view, built


def launch_debug_run(state: Any, script: dict[str, Any], who: str, *, keep_going: bool = False) -> str:
    """Start a debug replay of the script as it is now; return the run id."""
    view, built = _replay_plan(state, script, who, keep_going=keep_going)
    script_id = script["id"]
    run_id = f"d_{uuid.uuid4().hex[:10]}"
    state.db.insert_debug_run(run_id, script_id, who)

    def finish(rid: str, outcome: Outcome) -> None:
        summary = debug_runner.summarize(outcome.paths["jtl"], script, view, built.unresolved, built.warnings)
        status, error = finish_status(outcome, has_results=any(i["state"] == "ran" for i in summary["items"]))
        state.db.update_debug_run(rid, status=status, error=error, exit_code=outcome.exit_code,
                                  finished_at=now_iso(), summary_json=json.dumps(summary))
        for old in state.db.prune_debug_runs(script_id, debug_runner.KEEP_RUNS):
            shutil.rmtree(run_paths(state.settings, old)["dir"], ignore_errors=True)

    try:
        state.executor.launch(run_id, built.xml, duration_seconds=DEBUG_MAX_SECONDS, files=built.files,
                              properties=debug_runner.PROPERTIES, report=False, finish=finish,
                              on_start=lambda rid: state.db.update_debug_run(rid, started_at=now_iso()))
    except TooManyTests as exc:
        state.db.update_debug_run(run_id, status="failed", error=str(exc), finished_at=now_iso())
        raise HTTPException(429, str(exc)) from exc
    audit.info("DEBUG script=%s run=%s by=%s", script_id, run_id, who)
    return run_id


@router.post("/scripts/{script_id}/debug-run", status_code=201)
def start_debug_run(script_id: str, request: Request, who: str = Depends(tester)):
    run_id = launch_debug_run(request.app.state, get_script_or_404(request, script_id), who)
    return {"id": run_id, "status": "running"}


def _brief(run: dict[str, Any]) -> dict[str, Any]:
    s = run.get("summary") or {}
    return {k: run[k] for k in ("id", "script_id", "status", "created_at", "started_at", "finished_at", "error")} | {
        "verdict": s.get("verdict"), "passed": s.get("passed"), "failed": s.get("failed"),
        "first_failure": s.get("first_failure"),
    }


@router.get("/scripts/{script_id}/debug-runs")
def list_debug_runs(script_id: str, request: Request, _: str = Depends(require_api_key)):
    get_script_or_404(request, script_id)
    return [_brief(r) for r in request.app.state.db.list_debug_runs(script_id)]


def _get_run(request: Request, run_id: str) -> dict[str, Any]:
    run = request.app.state.db.get_debug_run(run_id)
    if not run:
        raise HTTPException(404, f"Debug run '{run_id}' not found")
    script = request.app.state.db.get_script(run["script_id"])
    ensure_project(request, script.get("project_id") if script else None)
    return run


@router.get("/debug-runs/{run_id}")
def get_debug_run(run_id: str, request: Request, _: str = Depends(require_api_key)):
    return _get_run(request, run_id)


@router.get("/debug-runs/{run_id}/samples/{index}")
def get_debug_sample(run_id: str, index: int, request: Request,
                     reveal: bool = Query(False, description="show credential headers unmasked"),
                     who: str = Depends(require_api_key)):
    _get_run(request, run_id)
    if reveal and not who.can("tester"):
        raise HTTPException(403, "Showing credentials needs the tester role")
    path = run_paths(request.app.state.settings, run_id)["jtl"]
    detail = debug_runner.sample_detail(path, index, reveal=reveal)
    if detail is None:
        raise HTTPException(404, f"Request #{index} has no result in this debug run")
    if reveal:
        audit.info("REVEAL debug=%s request=%d by=%s", run_id, index, who)
    return detail


# ---- correlate automatically ------------------------------------------------------------------------

class _Stop(Exception):
    """Ends an automatic correlation with a message for the user."""


class AutoCorrelation(threading.Thread):
    """Replay, find the sources of dynamic values, add them; repeat while new rules turn up."""

    def __init__(self, state: Any, script_id: str, who: str):
        super().__init__(name=f"autocorrelate-{script_id}", daemon=True)
        self.state, self.script_id, self.who = state, script_id, who
        self.progress: dict[str, Any] = {
            "status": "running", "round": 0, "max_replays": AUTO_MAX_REPLAYS, "replays": [], "correlations": [],
            "parameters": [], "suggested": 0, "unknown": 0, "message": "Starting the first replay",
            "started_at": now_iso(), "finished_at": None, "by": str(who),
        }

    def run(self) -> None:
        try:
            for n in range(1, AUTO_MAX_REPLAYS + 1):
                script = self.state.db.get_script(self.script_id)
                if script is None:
                    raise _Stop("The script was deleted.")
                self.progress.update(round=n, message=f"Replay {n}: running the script once")
                run_id = launch_debug_run(self.state, script, self.who, keep_going=True)
                self.progress["replays"].append(run_id)
                self._wait(run_id)
                run = self.state.db.get_debug_run(run_id) or {}
                summary = run.get("summary") or {}
                if not any(i.get("state") == "ran" for i in summary.get("items", [])):
                    raise _Stop(f"The replay did not run: {run.get('error') or 'open the debug replay for details'}")
                self.progress["message"] = f"Replay {n}: matching recorded values with the responses"
                if not self._scan_and_apply(script, run_id):
                    self._finish("done", self._verdict(summary, script))
                    return
            self._finish("done", f"Stopped after {AUTO_MAX_REPLAYS} replays while rules were still being added: "
                                 "run a debug replay to check the latest ones.")
        except HTTPException as exc:
            self._finish("failed", str(exc.detail))
        except _Stop as exc:
            self._finish("failed", str(exc))
        except Exception as exc:  # noqa: BLE001 - reported on the page
            log.exception("automatic correlation of %s failed", self.script_id)
            self._finish("failed", f"Automatic correlation failed: {exc}")

    def _wait(self, run_id: str) -> None:
        deadline = time.monotonic() + DEBUG_MAX_SECONDS + 120
        while self.state.executor.is_running(run_id):
            if time.monotonic() > deadline:
                self.state.executor.stop(run_id)
                raise _Stop("The replay took too long and was stopped.")
            time.sleep(0.25)

    def _scan_and_apply(self, script: dict[str, Any], run_id: str) -> bool:
        """Scan one replay; add what it found. True if anything was added."""
        _, view = evaluate(script)
        tree, _ = script_builder.build_tree(script, view)
        order = [i for t in view["transactions"] for i in t["items"]]
        requests = autocorrelate.script_requests(tree, order)
        replay = autocorrelate.replay_responses(debug_runner.parse_results(run_paths(self.state.settings, run_id)["jtl"]))
        taken = autocorrelate.design_names(script["design"], script.get("variables") or [])
        taken |= {v.lower() for v in script_builder.plan_variables(tree)}
        found = autocorrelate.scan(requests, replay, taken)
        parameters, replacements, suggestions = autocorrelate.generated_parameters(found["generated"], taken)
        added: dict[str, list[str]] = {}

        def mutate(design: dict[str, Any]) -> dict[str, Any]:
            merged, result = autocorrelate.merge(design, correlations=found["correlations"], parameters=parameters,
                                                 replacements=replacements)
            added.update(result)
            return merged

        self.state.db.update_design(self.script_id, mutate)
        if suggestions:
            def suggest(current: dict[str, Any]) -> dict[str, Any]:
                known = {(s.get("name"), s.get("preview")) for s in current.get("parameters") or []}
                fresh = [s for s in suggestions if (s["name"], s["preview"]) not in known]
                return {**current, "parameters": [*(current.get("parameters") or []), *fresh]}
            self.state.db.update_suggestions(self.script_id, suggest)
        self.progress["correlations"] += added.get("correlations", [])
        self.progress["parameters"] += added.get("parameters", [])
        self.progress["suggested"] += len(suggestions)
        self.progress["unknown"] = found["unknown"]
        return bool(added.get("correlations") or added.get("parameters"))

    def _verdict(self, summary: dict[str, Any], script: dict[str, Any]) -> str:
        if summary.get("verdict") == "pass":
            return "The last replay passed."
        first = summary.get("first_failure")
        row = next((i for i in summary.get("items", []) if i["index"] == first), None)
        where = f"#{first} {script['items'][first]['label']}" if first is not None and first < len(script["items"]) else "a request"
        cause = (row or {}).get("diagnosis") or {}
        hint = f" Likely cause: {cause['cause']}" if cause.get("cause") else ""
        unknown = (f" {self.progress['unknown']} recorded value(s) could not be traced because an earlier request "
                   "failed.") if self.progress["unknown"] else ""
        return f"The last replay still fails at {where}.{hint}{unknown}"

    def _finish(self, status: str, message: str) -> None:
        self.progress.update(status=status, message=message, finished_at=now_iso())
        record = dict(self.progress)
        self.state.db.update_suggestions(self.script_id, lambda s: {**s, "autocorrelate": record})
        audit.info("AUTOCORRELATE script=%s by=%s status=%s replays=%d correlations=%d parameters=%d", self.script_id,
                   self.who, status, len(record["replays"]), len(record["correlations"]), len(record["parameters"]))


@router.post("/scripts/{script_id}/autocorrelate", status_code=202)
def start_autocorrelate(script_id: str, request: Request, who: str = Depends(tester)):
    """Replay the script (one user, up to four times) and add the correlations the replays reveal."""
    state = request.app.state
    script = get_script_or_404(request, script_id)
    job = state.autocorrelations.get(script_id)
    if job is not None and job.is_alive():
        raise HTTPException(409, "Automatic correlation is already running for this script")
    _replay_plan(state, script, who, keep_going=True)        # fail now on what would stop the first replay
    job = AutoCorrelation(state, script_id, who)
    state.autocorrelations[script_id] = job
    job.start()
    audit.info("AUTOCORRELATE start script=%s by=%s", script_id, who)
    return dict(job.progress)


@router.get("/scripts/{script_id}/autocorrelate")
def autocorrelate_status(script_id: str, request: Request, _: str = Depends(require_api_key)):
    script = get_script_or_404(request, script_id)
    job = request.app.state.autocorrelations.get(script_id)
    if job is not None and job.is_alive():
        return dict(job.progress)
    return (script.get("suggestions") or {}).get("autocorrelate") or {"status": "idle"}
