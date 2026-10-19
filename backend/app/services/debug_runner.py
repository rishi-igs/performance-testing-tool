"""Execute and inspect small, isolated debug runs for imported HAR flows."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from ..config import Settings
from ..db import Database, now_iso
from ..models.test_config import SENSITIVE_HEADERS
from ..services.test_executor import TooManyTests, TestExecutor

log = logging.getLogger("perf.debug")
_SECRET_KEY = re.compile(r"password|passwd|secret|token|authorization|cookie|csrf|api.?key", re.IGNORECASE)
_SECRET_HEADER = SENSITIVE_HEADERS | {"x-csrf-token", "x-xsrf-token"}
_PATH_ID = re.compile(r"^(?:\d{5,}|[0-9a-f]{8}-[0-9a-f-]{27,})$", re.IGNORECASE)
MAX_DEBUG_SECONDS = 300


def debug_paths(settings: Settings, run_id: str) -> dict[str, Path]:
    base = settings.runs_dir / run_id
    return {
        "dir": base,
        "plan": base / "debug_plan.jmx",
        "jtl": base / "debug_results.jtl",
        "log": base / "jmeter.log",
        "stdout": base / "jmeter_stdout.log",
    }


def debug_jmeter_properties() -> list[str]:
    return [
        "-Jjmeter.save.saveservice.output_format=xml",
        "-Jjmeter.save.saveservice.response_data=true",
        "-Jjmeter.save.saveservice.samplerData=true",
        "-Jjmeter.save.saveservice.requestHeaders=true",
        "-Jjmeter.save.saveservice.responseHeaders=true",
        "-Jjmeter.save.saveservice.assertions=true",
        "-Jjmeter.save.saveservice.subresults=true",
        "-Jjmeter.save.saveservice.url=true",
        "-Jjmeter.save.saveservice.timestamp_format=ms",
    ]


def _mask_json(text: str) -> str:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return re.sub(
            r'(?i)\b(password|passwd|secret|token|authorization|cookie|csrf|api[_-]?key)\b'
            r'(\s*[:=]\s*)(["\']?)[^&\s,"\']+',
            r"\1\2********",
            text,
        )

    def mask(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ("********" if _SECRET_KEY.search(key) else mask(child))
                    for key, child in value.items()}
        if isinstance(value, list):
            return [mask(child) for child in value]
        return value

    return json.dumps(mask(value), ensure_ascii=False, indent=2)


def _number(value: str) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _mask_headers(text: str) -> str:
    lines = []
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() in _SECRET_HEADER:
            lines.append(f"{name}: ********")
        else:
            lines.append(line)
    return "\n".join(lines)


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        path = "/".join("<redacted>" if _PATH_ID.fullmatch(part) else part
                        for part in parts.path.split("/"))
        query = "&".join(f"{key}=<redacted>" for key, _ in parse_qsl(parts.query, keep_blank_values=True))
        return urlunsplit((parts.scheme, parts.hostname or "", path, query, ""))
    except ValueError:
        return "<redacted URL>"


def parse_debug_samples(path: Path, script: dict[str, Any]) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    samples = []
    for element in root.iter():
        if element.tag != "httpSample":
            continue
        def child_text(name: str) -> str:
            child = element.find(name)
            return child.text or "" if child is not None else ""

        assertion_results = []
        for result in element.findall("assertionResult"):
            failed = (result.findtext("failure") or "false").lower() == "true"
            error = (result.findtext("error") or "false").lower() == "true"
            assertion_results.append({
                "name": result.findtext("name") or "Assertion",
                "passed": not failed and not error,
                "message": result.findtext("failureMessage") or "",
            })
        raw_url = element.get("url", "")
        sampler_data = child_text("samplerData")
        request_headers = _mask_headers(child_text("requestHeader"))
        response_headers = _mask_headers(child_text("responseHeader"))
        request_body = sampler_data.split("\n\n", 1)[1] if "\n\n" in sampler_data else ""
        response_body = child_text("responseData")
        request_body = _mask_json(request_body)
        response_body = _mask_json(response_body)
        code = element.get("rc", element.get("responseCode", ""))
        success = element.get("s", element.get("success", "false")).lower() == "true" and all(
            result["passed"] for result in assertion_results
        )
        samples.append({
            "index": len(samples),
            "label": element.get("lb", ""),
            "method": sampler_data.split(" ", 1)[0] if sampler_data else "",
            "url": _safe_url(raw_url),
            "request": {
                "headers": request_headers,
                "body": request_body,
                "cookies": "Cookies are masked in request headers.",
            },
            "response": {
                "status_code": code,
                "headers": response_headers,
                "body": response_body,
            },
            "timing": {
                "connect_ms": _number(element.get("ct", "0")),
                "latency_ms": _number(element.get("lt", "0")),
                "total_ms": _number(element.get("t", "0")),
            },
            "assertions": assertion_results,
            "success": success,
            "failure": element.get("failureMessage", "") or child_text("failureMessage"),
            "variables": [
                {"name": rule["variable"], "source_index": rule["source_index"],
                 "targets": rule["target_indices"], "value": "Captured by JMeter; value masked."}
                for rule in script["correlations"] if rule.get("enabled", True)
            ],
        })
    return samples


class DebugRunExecutor:
    def __init__(self, settings: Settings, db: Database, shared_executor: TestExecutor):
        self.settings = settings
        self.db = db
        self.shared_executor = shared_executor
        self._threads: dict[str, threading.Thread] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def start(self, run_id: str, script: dict[str, Any], plan: str, csv_data: bytes | None) -> None:
        self.shared_executor.reserve_external_slot(run_id)
        paths = debug_paths(self.settings, run_id)
        try:
            paths["dir"].mkdir(parents=True, exist_ok=False)
            paths["plan"].write_text(plan, encoding="utf-8")
            os.chmod(paths["plan"], 0o600)
            if csv_data is not None:
                data_path = paths["dir"] / "parameters.csv"
                data_path.write_bytes(csv_data)
                os.chmod(data_path, 0o600)
            thread = threading.Thread(
                target=self._run, args=(run_id, script, paths), daemon=True,
                name=f"debug-{run_id}",
            )
            with self._lock:
                self._threads[run_id] = thread
            thread.start()
        except Exception:
            self.shared_executor.release_external_slot(run_id)
            shutil.rmtree(paths["dir"], ignore_errors=True)
            raise

    def _launcher(self) -> list[str]:
        if self.settings.jmeter_bin.lower().endswith(".py"):
            if not Path(self.settings.jmeter_bin).exists():
                raise FileNotFoundError(f"JMeter was not found ('{self.settings.jmeter_bin}').")
            return [sys.executable, self.settings.jmeter_bin]
        if shutil.which(self.settings.jmeter_bin) is None:
            raise FileNotFoundError(
                f"JMeter was not found ('{self.settings.jmeter_bin}'). "
                "Install Apache JMeter or set JMETER_BIN to its full path."
            )
        return ["cmd.exe", "/c", self.settings.jmeter_bin]

    def _run(self, run_id: str, script: dict[str, Any], paths: dict[str, Path]) -> None:
        self.db.update_debug_run(run_id, status="running", started_at=now_iso())
        process = None
        error = None
        try:
            env = os.environ.copy()
            env.pop("JMETER_BIN", None)
            with paths["stdout"].open("wb") as output:
                process = subprocess.Popen(
                    [*self._launcher(), "-n", "-t", str(paths["plan"]), "-l", str(paths["jtl"]),
                     "-j", str(paths["log"]), *debug_jmeter_properties()],
                    cwd=paths["dir"], env=env, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                with self._lock:
                    self._processes[run_id] = process
                try:
                    exit_code = process.wait(timeout=MAX_DEBUG_SECONDS)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        exit_code = process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        exit_code = process.wait(timeout=5)
                    error = f"Debug run exceeded the {MAX_DEBUG_SECONDS}-second limit."
            if exit_code != 0 and error is None:
                error = f"JMeter exited with code {exit_code}. Check the debug JMeter log."
        except FileNotFoundError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("debug run %s failed", run_id)
            error = f"Could not run debug plan: {exc}"
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    log.exception("could not stop JMeter process for debug run %s", run_id)
                    error = error or "Could not stop the JMeter debug process."
            with self._lock:
                self._processes.pop(run_id, None)
            try:
                csv_path = paths["dir"] / "parameters.csv"
                if csv_path.exists():
                    csv_path.unlink()
            except OSError:
                error = error or "Could not remove the temporary CSV used by this debug run."
                log.exception("could not remove temporary CSV for debug run %s", run_id)
            try:
                samples = parse_debug_samples(paths["jtl"], script)
                summary = {
                    "samples": len(samples),
                    "passed": sum(sample["success"] for sample in samples),
                    "failed": sum(not sample["success"] for sample in samples),
                    "assertion_failures": sum(
                        not assertion["passed"]
                        for sample in samples
                        for assertion in sample["assertions"]
                    ),
                }
                self.db.update_debug_run(
                    run_id, status="failed" if error else "completed",
                    summary_json=json.dumps(summary), error=error, finished_at=now_iso(),
                )
            except Exception as exc:  # noqa: BLE001
                error = error or f"Could not finalize debug run: {exc}"
                log.exception("could not finalize debug run %s", run_id)
                try:
                    self.db.update_debug_run(
                        run_id, status="failed", error=error, finished_at=now_iso(),
                    )
                except Exception:  # noqa: BLE001
                    log.exception("could not persist failed finalization for debug run %s", run_id)
            finally:
                with self._lock:
                    self._threads.pop(run_id, None)
                self.shared_executor.release_external_slot(run_id)

    def shutdown(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            if process.poll() is None:
                process.terminate()
