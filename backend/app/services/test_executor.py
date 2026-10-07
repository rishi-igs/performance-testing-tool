"""Run JMeter headlessly as a background process and track its lifecycle."""
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
from pathlib import Path

from ..config import Settings
from ..db import Database, now_iso
from ..models.test_config import LoadTestConfig
from . import result_analyzer
from .jmeter_plan_builder import build_plan, jmeter_properties

log = logging.getLogger("perf.executor")

KILL_GRACE_SECONDS = 15
RUNTIME_SLACK_SECONDS = 900


class TooManyTests(RuntimeError):
    pass


@dataclass
class _Run:
    config: LoadTestConfig
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
        with self._lock:
            active = sum(
                1
                for r in self._runs.values()
                if r.thread and r.thread.is_alive()
            )

            if active >= self.settings.max_concurrent_tests:
                raise TooManyTests(
                    f"{active} tests are already running "
                    f"(limit {self.settings.max_concurrent_tests})"
                )

            paths = run_paths(self.settings, test_id)

            paths["dir"].mkdir(
                parents=True,
                exist_ok=True,
            )

            # The plan may contain header secrets, so keep it
            # readable by the owner only.
            paths["plan"].write_text(
                build_plan(config),
                encoding="utf-8",
            )

            os.chmod(
                paths["plan"],
                0o600,
            )

            run = _Run(config=config)

            run.thread = threading.Thread(
                target=self._run,
                args=(test_id, run, paths),
                daemon=True,
                name=f"test-{test_id}",
            )

            self._runs[test_id] = run
            run.thread.start()

    def stop(self, test_id: str) -> bool:
        """Request a stop. Returns False if the test is not running here."""

        with self._lock:
            run = self._runs.get(test_id)

            if (
                not run
                or not run.thread
                or not run.thread.is_alive()
            ):
                return False

            run.stop_requested = True
            proc = run.proc

            if (
                proc
                and proc.poll() is None
                and run.term_at is None
            ):
                run.term_at = time.monotonic()
                self._signal(
                    proc,
                    signal.SIGTERM,
                )

        return True

    def is_running(self, test_id: str) -> bool:
        run = self._runs.get(test_id)

        return bool(
            run
            and run.thread
            and run.thread.is_alive()
        )

    def shutdown(self) -> None:
        for test_id in list(self._runs):
            self.stop(test_id)

    # ---- internals --------------------------------------------------------

    def _command(self, paths: dict[str, Path]) -> list[str]:
        """
        Build the Windows command used to launch JMeter.

        JMETER_BIN points to jmeter.bat, so Windows needs
        cmd.exe to execute the batch file.
        """

        launcher = self._launcher()
        return [
            *launcher,
            "-n",
            "-t",
            str(paths["plan"]),
            "-l",
            str(paths["jtl"]),
            "-j",
            str(paths["log"]),
            "-e",
            "-o",
            str(paths["report"]),
            *jmeter_properties(),
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
    def _signal(
        proc: subprocess.Popen,
        sig: int,
    ) -> None:
        """
        Send a signal to the JMeter process.

        Windows does not provide os.killpg(), so use
        Popen.terminate() and Popen.kill() on Windows.
        """

        try:
            if os.name == "nt":
                if sig == signal.SIGTERM:
                    proc.terminate()

                elif sig == signal.SIGKILL:
                    proc.kill()

            else:
                os.killpg(
                    os.getpgid(proc.pid),
                    sig,
                )

        except (
            ProcessLookupError,
            PermissionError,
        ):
            pass

    def _run(
        self,
        test_id: str,
        run: _Run,
        paths: dict[str, Path],
    ) -> None:

        cfg = run.config
        started = time.monotonic()

        self.db.update_test(
            test_id,
            started_at=now_iso(),
        )

        exit_code: int | None = None
        error: str | None = None

        try:
            with paths["stdout"].open("wb") as out:

                proc = subprocess.Popen(
                    self._command(paths),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    cwd=paths["dir"],
                    env=self._jmeter_environment(),
                    start_new_session=True,
                )

                with self._lock:
                    run.proc = proc

                    if (
                        run.stop_requested
                        and run.term_at is None
                    ):
                        run.term_at = time.monotonic()

                        self._signal(
                            proc,
                            signal.SIGTERM,
                        )

                deadline = (
                    started
                    + cfg.duration_seconds
                    + RUNTIME_SLACK_SECONDS
                )

                while proc.poll() is None:

                    now = time.monotonic()

                    if (
                        now > deadline
                        and not run.stop_requested
                    ):
                        error = (
                            "Test exceeded its maximum runtime "
                            "and was stopped"
                        )

                        self.stop(test_id)

                    # A stop was requested but JMeter did not
                    # exit in time: force it.
                    if (
                        run.term_at
                        and not run.killed
                        and now - run.term_at
                        > KILL_GRACE_SECONDS
                    ):
                        run.killed = True

                        self._signal(
                            proc,
                            signal.SIGKILL,
                        )

                    time.sleep(0.2)

                exit_code = proc.returncode

        except FileNotFoundError:

            error = (
                f"JMeter was not found "
                f"('{self.settings.jmeter_bin}'). "
                "Install Apache JMeter or set JMETER_BIN "
                "to its full path."
            )

        except Exception as exc:

            log.exception(
                "test %s crashed",
                test_id,
            )

            error = f"Could not run JMeter: {exc}"

        finally:

            self._finish(
                test_id,
                run,
                paths,
                exit_code,
                error,
            )

    def _finish(
        self,
        test_id: str,
        run: _Run,
        paths: dict[str, Path],
        exit_code: int | None,
        error: str | None,
    ) -> None:

        # If we stopped early, the HTML report may be missing.
        # Build it from the partial results.
        if (
            run.stop_requested
            and paths["jtl"].exists()
            and not paths["report"].exists()
        ):
            self._generate_report(paths)

        samples = result_analyzer.parse_jtl(
            paths["jtl"]
        )

        summary = result_analyzer.summarize(
            samples,
            run.config.thresholds,
        )

        if (
            run.stop_requested
            and error is None
        ):
            status = "stopped"

        elif error or exit_code != 0:

            status = "failed"

            if error is None:
                error = (
                    f"JMeter exited with code {exit_code}. "
                    "See jmeter_stdout.log."
                )

        else:
            status = "completed"

        if (
            status == "completed"
            and not samples
        ):
            status = "failed"

            error = (
                "JMeter finished but produced no results. "
                "Check jmeter.log."
            )

        self.db.update_test(
            test_id,
            status=status,
            exit_code=exit_code,
            error=error,
            finished_at=now_iso(),
            summary_json=json.dumps(summary),
        )

        log.info(
            "test %s finished: %s",
            test_id,
            status,
        )

    def _generate_report(
        self,
        paths: dict[str, Path],
    ) -> None:
        """
        Generate the HTML report from the JTL file.

        On Windows, JMETER_BIN points to jmeter.bat,
        so execute it through cmd.exe.
        """

        try:

            subprocess.run(
                [
                    *self._launcher(),
                    "-g",
                    str(paths["jtl"]),
                    "-o",
                    str(paths["report"]),
                ],
                cwd=paths["dir"],
                env=self._jmeter_environment(),
                timeout=120,
                capture_output=True,
                check=False,
            )

        except (
            OSError,
            subprocess.SubprocessError,
        ):

            log.warning(
                "could not generate HTML report "
                "from partial results"
            )