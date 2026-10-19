"""Run JMeter headlessly as a background process and track its lifecycle.

Every kind of run (quick test, debug replay, scenario) goes through launch(): write the plan
and its data files, start JMeter, enforce stop requests and the maximum runtime, then hand the
outcome to the caller's finish callback, which parses the results and stores the status.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from ..config import Settings
from ..db import Database, now_iso
from ..models.test_config import LoadTestConfig, Thresholds
from . import result_analyzer
from .jmeter_plan_builder import build_plan, jmeter_properties

log = logging.getLogger("perf.executor")

KILL_GRACE_SECONDS = 15
RUNTIME_SLACK_SECONDS = 900


class TooManyTests(RuntimeError):
    pass


@dataclass
class Outcome:
    exit_code: int | None
    error: str | None
    stopped: bool
    paths: dict[str, Path]


@dataclass
class _Run:
    duration_seconds: int
    finish: Callable[[str, Outcome], None]
    on_start: Callable[[str], None] | None = None
    report: bool = True
    properties: tuple[str, ...] = ()
    proc: subprocess.Popen | None = None
    stop_requested: bool = False
    term_at: float | None = None
    killed: bool = False
    thread: threading.Thread | None = field(default=None, repr=False)


def run_paths(settings: Settings, test_id: str) -> dict[str, Path]:
    base = settings.runs_dir / test_id
    return {
        "dir": base,
        "plan": base / "test_plan.jmx",
        "jtl": base / "results.jtl",
        "log": base / "jmeter.log",
        "stdout": base / "jmeter_stdout.log",
        "report": base / "html_report",
    }


class TestExecutor:
    __test__ = False

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    # ---- public API -------------------------------------------------------

    def start(self, test_id: str, config: LoadTestConfig) -> None:
        """Quick test: one request from a LoadTestConfig."""
        thresholds = config.thresholds
        self.launch(test_id, build_plan(config), duration_seconds=config.duration_seconds,
                    finish=lambda run_id, outcome: self._finish_test(run_id, outcome, thresholds),
                    on_start=lambda run_id: self.db.update_test(run_id, started_at=now_iso()))

    def launch(
        self,
        run_id: str,
        plan_xml: str,
        *,
        duration_seconds: int,
        finish: Callable[[str, Outcome], None],
        on_start: Callable[[str], None] | None = None,
        files: dict[str, bytes] | None = None,
        properties: tuple[str, ...] = (),
        report: bool = True,
    ) -> None:
        with self._lock:
            active = sum(1 for r in self._runs.values() if r.thread and r.thread.is_alive())
            if active >= self.settings.max_concurrent_tests:
                raise TooManyTests(
                    f"{active} tests are already running (limit {self.settings.max_concurrent_tests})"
                )

            paths = run_paths(self.settings, run_id)
            paths["dir"].mkdir(parents=True, exist_ok=True)
            # The plan may contain header secrets, so keep it readable by the owner only.
            paths["plan"].write_text(plan_xml, encoding="utf-8")
            os.chmod(paths["plan"], 0o600)
            for name, content in (files or {}).items():
                target = paths["dir"] / _safe_relative(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

            run = _Run(duration_seconds=duration_seconds, finish=finish, on_start=on_start, report=report,
                       properties=properties)
            run.thread = threading.Thread(target=self._run, args=(run_id, run, paths), daemon=True,
                                          name=f"test-{run_id}")
            self._runs[run_id] = run
            run.thread.start()

    def stop(self, test_id: str) -> bool:
        """Request a stop. Returns False if the test is not running here."""
        with self._lock:
            run = self._runs.get(test_id)
            if not run or not run.thread or not run.thread.is_alive():
                return False
            run.stop_requested = True
            proc = run.proc
            if proc and proc.poll() is None and run.term_at is None:
                run.term_at = time.monotonic()
                self._signal(proc, signal.SIGTERM)
        return True

    def is_running(self, test_id: str) -> bool:
        run = self._runs.get(test_id)
        return bool(run and run.thread and run.thread.is_alive())

    def shutdown(self) -> None:
        for test_id in list(self._runs):
            self.stop(test_id)

    # ---- internals --------------------------------------------------------

    def _command(self, paths: dict[str, Path], run: _Run) -> list[str]:
        """Command line for a headless run.

        On Windows JMETER_BIN points to jmeter.bat, so the launcher is cmd.exe.
        """
        report = ["-e", "-o", str(paths["report"])] if run.report else []
        return [
            *self._launcher(),
            "-n", "-t", str(paths["plan"]), "-l", str(paths["jtl"]), "-j", str(paths["log"]),
            *report, *jmeter_properties(), *run.properties,
        ]

    def _launcher(self) -> list[str]:
        if self.settings.jmeter_bin.lower().endswith(".py"):
            return [sys.executable, self.settings.jmeter_bin]
        return ["cmd.exe", "/c", self.settings.jmeter_bin]

    @staticmethod
    def _jmeter_environment() -> dict[str, str]:
        # JMETER_BIN is also reserved by JMeter's Windows launcher for its bin directory.
        env = os.environ.copy()
        env.pop("JMETER_BIN", None)
        return env

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        """Send a signal to the JMeter process.

        Windows does not provide os.killpg(), so use Popen.terminate() and Popen.kill() there.
        """
        try:
            if os.name == "nt":
                if sig == signal.SIGTERM:
                    proc.terminate()
                elif sig == signal.SIGKILL:
                    proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _run(self, test_id: str, run: _Run, paths: dict[str, Path]) -> None:
        started = time.monotonic()
        exit_code: int | None = None
        error: str | None = None
        if run.on_start:
            run.on_start(test_id)
        try:
            jmeter = self.settings.jmeter_bin
            if any(sep in jmeter for sep in ("/", "\\")) and not Path(jmeter).exists():
                raise FileNotFoundError(jmeter)    # on Windows cmd.exe would only say "exited with code 1"
            with paths["stdout"].open("wb") as out:
                proc = subprocess.Popen(
                    self._command(paths, run), stdout=out, stderr=subprocess.STDOUT, cwd=paths["dir"],
                    env=self._jmeter_environment(), start_new_session=True,
                )
                with self._lock:
                    run.proc = proc
                    if run.stop_requested and run.term_at is None:
                        run.term_at = time.monotonic()
                        self._signal(proc, signal.SIGTERM)

                deadline = started + run.duration_seconds + RUNTIME_SLACK_SECONDS
                while proc.poll() is None:
                    now = time.monotonic()
                    if now > deadline and not run.stop_requested:
                        error = "Test exceeded its maximum runtime and was stopped"
                        self.stop(test_id)
                    # A stop was requested but JMeter did not exit in time: force it.
                    if run.term_at and not run.killed and now - run.term_at > KILL_GRACE_SECONDS:
                        run.killed = True
                        self._signal(proc, signal.SIGKILL)
                    time.sleep(0.2)
                exit_code = proc.returncode
        except FileNotFoundError:
            error = (f"JMeter was not found ('{self.settings.jmeter_bin}'). "
                     "Install Apache JMeter or set JMETER_BIN to its full path.")
        except Exception as exc:  # noqa: BLE001
            log.exception("test %s crashed", test_id)
            error = f"Could not run JMeter: {exc}"
        finally:
            # If we stopped early, the HTML report may be missing. Build it from the partial results.
            if run.report and run.stop_requested and paths["jtl"].exists() and not paths["report"].exists():
                self._generate_report(paths)
            try:
                run.finish(test_id, Outcome(exit_code=exit_code, error=error, stopped=run.stop_requested, paths=paths))
            except Exception:  # noqa: BLE001
                log.exception("finishing run %s failed", test_id)

    def _finish_test(self, test_id: str, outcome: Outcome, thresholds: Thresholds) -> None:
        samples = result_analyzer.parse_jtl(outcome.paths["jtl"])
        summary = result_analyzer.summarize(samples, thresholds)
        status, error = finish_status(outcome, has_results=bool(samples))
        self.db.update_test(test_id, status=status, exit_code=outcome.exit_code, error=error,
                            finished_at=now_iso(), summary_json=json.dumps(summary))
        log.info("test %s finished: %s", test_id, status)

    def _generate_report(self, paths: dict[str, Path]) -> None:
        """Generate the HTML report from the JTL file (jmeter.bat runs through cmd.exe on Windows)."""
        try:
            subprocess.run(
                [*self._launcher(), "-g", str(paths["jtl"]), "-o", str(paths["report"])],
                cwd=paths["dir"], env=self._jmeter_environment(), timeout=120, capture_output=True, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            log.warning("could not generate HTML report from partial results")


def finish_status(outcome: Outcome, *, has_results: bool) -> tuple[str, str | None]:
    """(status, error) for a finished run: stopped, failed or completed."""
    error = outcome.error
    if outcome.stopped and error is None:
        return "stopped", None
    if error or outcome.exit_code != 0:
        return "failed", error or f"JMeter exited with code {outcome.exit_code}. See jmeter_stdout.log."
    if not has_results:
        return "failed", "JMeter finished but produced no results. Check jmeter.log."
    return "completed", None


def _safe_relative(name: str) -> Path:
    """A data file path inside the run folder (no absolute paths or ..)."""
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or ".." in parts or ":" in name:
        raise ValueError(f"invalid data file path {name!r}")
    return Path(*parts)
