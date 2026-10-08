"""Server monitors: CPU, memory and other metrics collected while a test runs.

Three kinds of monitor:
  agent       the console's own agent (backend/agent/perf_agent.py) on the server: /metrics.json
  prometheus  any Prometheus endpoint (node_exporter, windows_exporter, the application's own
              /metrics): presets for CPU and memory, or chosen metrics as gauges or rates
  local       the machine running the console (useful when it also runs JMeter or the target)

A sampler thread polls the run's monitors until the run ends and appends to monitors.csv in the
run folder:  timestamp_ms,monitor,metric,label,unit,value
"""
from __future__ import annotations

import csv
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..models.infra import MonitorIn, PromMetric

log = logging.getLogger("perf.monitoring")

MONITORS_FILE = "monitors.csv"
ERRORS_FILE = "monitor_errors.json"
COLUMNS = ["timestamp_ms", "monitor", "metric", "label", "unit", "value"]
AGENT_METRICS = [("cpu_percent", "CPU", "%"), ("memory_percent", "Memory used", "%"), ("disk_percent", "Disk used", "%"),
                 ("load1", "Load (1 min)", ""), ("net_rx_kbps", "Network in", "KB/s"), ("net_tx_kbps", "Network out", "KB/s")]
TIMEOUT_SECONDS = 4
_SAMPLE_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text: str) -> list[tuple[str, dict[str, str], float]]:
    """(metric name, labels, value) for each sample line of the text exposition format."""
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_LINE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        labels = {k: v.replace('\\"', '"').replace("\\\\", "\\") for k, v in _LABEL.findall(m.group(2) or "")}
        out.append((m.group(1), labels, value))
    return out


def _sum(samples: list[tuple[str, dict[str, str], float]], metric: str, labels: dict[str, str] | None = None) -> float | None:
    values = [v for name, lab, v in samples if name == metric and all(lab.get(k) == want for k, want in (labels or {}).items())]
    return sum(values) if values else None


def _count(samples, metric: str, label: str, labels: dict[str, str]) -> int:
    return len({lab.get(label) for name, lab, _ in samples
                if name == metric and all(lab.get(k) == v for k, v in labels.items())})


class MonitorReader:
    """Reads one monitor; keeps the previous sample to turn counters into rates."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg = MonitorIn(**config)
        self._previous: dict[str, tuple[float, float]] = {}
        self._local = None

    def _fetch(self, url: str, headers: dict[str, str]) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "perf-console-monitor", **headers})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:    # noqa: S310 - URL checked on save
            return resp.read(5_000_000)

    def _rate(self, key: str, counter: float | None, now: float) -> float | None:
        if counter is None:
            return None
        before = self._previous.get(key)
        self._previous[key] = (now, counter)
        if not before or now <= before[0] or counter < before[1]:
            return None
        return (counter - before[1]) / (now - before[0])

    def read(self) -> list[tuple[str, str, str, float]]:
        """(metric id, label, unit, value) for every value available right now."""
        if self.cfg.kind in ("agent", "local"):
            if self.cfg.kind == "local":
                if self._local is None:
                    from agent.perf_agent import Collector
                    self._local = Collector()
                data = self._local.sample()
            else:
                headers = {"X-Agent-Token": self.cfg.token} if self.cfg.token else {}
                data = json.loads(self._fetch(self.cfg.url.rstrip("/") + "/metrics.json"
                                              if not self.cfg.url.endswith("metrics.json") else self.cfg.url, headers))
            return [(key, label, unit, float(data[key])) for key, label, unit in AGENT_METRICS
                    if isinstance(data.get(key), (int, float))]
        body = self._fetch(self.cfg.url, {})
        # Stamp the sample when the exporter answered (its counters are from then), not before the
        # request: the first request's one-off setup would otherwise skew the first rate.
        now = time.time()
        samples = parse_prometheus(body.decode("utf-8", "replace"))
        out = []
        if self.cfg.preset == "node_exporter":
            idle = self._rate("idle", _sum(samples, "node_cpu_seconds_total", {"mode": "idle"}), now)
            cpus = _count(samples, "node_cpu_seconds_total", "cpu", {"mode": "idle"})
            if idle is not None and cpus:
                out.append(("cpu_percent", "CPU", "%", round(max(0.0, min(100.0, 100 * (1 - idle / cpus))), 1)))
            total = _sum(samples, "node_memory_MemTotal_bytes")
            available = _sum(samples, "node_memory_MemAvailable_bytes")
            if total and available is not None:
                out.append(("memory_percent", "Memory used", "%", round(100 * (1 - available / total), 1)))
            load = _sum(samples, "node_load1")
            if load is not None:
                out.append(("load1", "Load (1 min)", "", round(load, 2)))
        elif self.cfg.preset == "windows_exporter":
            idle = self._rate("idle", _sum(samples, "windows_cpu_time_total", {"mode": "idle"}), now)
            cpus = _count(samples, "windows_cpu_time_total", "core", {"mode": "idle"})
            if idle is not None and cpus:
                out.append(("cpu_percent", "CPU", "%", round(max(0.0, min(100.0, 100 * (1 - idle / cpus))), 1)))
            total = _sum(samples, "windows_cs_physical_memory_bytes")
            free = _sum(samples, "windows_os_physical_memory_free_bytes")
            if total and free is not None:
                out.append(("memory_percent", "Memory used", "%", round(100 * (1 - free / total), 1)))
        for m in self.cfg.metrics:
            value = _sum(samples, m.metric, m.labels)
            if m.type == "rate":
                value = self._rate(f"m:{m.id}", value, now)
            if value is not None:
                out.append((m.id, m.label, m.unit, round(value * m.scale, 4)))
        return out


def check_monitor(config: dict[str, Any]) -> dict[str, Any]:
    """Read a monitor twice (rates need two samples) and report what it returns."""
    reader = MonitorReader(config)
    try:
        reader.read()
        time.sleep(1.1)
        values = reader.read()
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)[:300], "values": [], "checked_at": time.time()}
    return {"ok": bool(values), "error": None if values else "the monitor returned no known metrics",
            "values": [{"metric": m, "label": label, "unit": unit, "value": v} for m, label, unit, v in values],
            "checked_at": time.time()}


class MonitorSampler(threading.Thread):
    """Polls monitors while `is_running()` is true and writes their samples to the run folder."""

    def __init__(self, run_dir: Path, monitors: list[dict[str, Any]], is_running: Callable[[], bool]) -> None:
        super().__init__(daemon=True, name=f"monitors-{run_dir.name}")
        self.run_dir = run_dir
        self.readers = [(m["config"]["name"], MonitorReader(m["config"])) for m in monitors]
        self.interval = min((MonitorIn(**m["config"]).interval_seconds for m in monitors), default=5)
        self.is_running = is_running
        self.stop_event = threading.Event()
        self.errors: dict[str, str] = {}

    def finish(self, timeout: float = 15) -> None:
        """Take a last sample and stop (called when the run ends)."""
        self.stop_event.set()
        self.join(timeout)

    def run(self) -> None:
        path = self.run_dir / MONITORS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(COLUMNS)
            # Wait for JMeter to start, then sample until it stops (and once more at the end).
            while time.monotonic() - started < 30 and not self.is_running() and not self.stop_event.is_set():
                time.sleep(0.2)
            while True:
                last = self.stop_event.is_set() or not self.is_running()
                ts = int(time.time() * 1000)
                for name, reader in self.readers:
                    try:
                        for metric, label, unit, value in reader.read():
                            writer.writerow([ts, name, metric, label, unit, value])
                    except Exception as exc:  # noqa: BLE001 - one bad monitor must not stop the others
                        self.errors[name] = str(exc)[:300]
                fh.flush()
                if last:
                    break
                deadline = time.monotonic() + self.interval
                while time.monotonic() < deadline and self.is_running() and not self.stop_event.is_set():
                    time.sleep(0.2)
        (self.run_dir / ERRORS_FILE).write_text(json.dumps(self.errors), encoding="utf-8")


def monitor_series(run_dir: Path) -> list[dict[str, Any]]:
    """The monitor samples of a run, as one series per monitor and metric."""
    path = run_dir / MONITORS_FILE
    if not path.exists():
        return []
    series: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                ts, value = int(row["timestamp_ms"]), float(row["value"])
            except (KeyError, ValueError, TypeError):
                continue
            key = (row["monitor"], row["metric"])
            s = series.setdefault(key, {"monitor": row["monitor"], "metric": row["metric"], "label": row["label"],
                                        "unit": row["unit"], "points": []})
            s["points"].append((ts, value))
    return list(series.values())


def monitor_errors(run_dir: Path) -> dict[str, str]:
    path = run_dir / ERRORS_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def monitor_warnings(series: list[dict[str, Any]], run_start_ms: int | None) -> list[dict[str, str]]:
    """Plain-language findings: sustained high CPU or memory, and memory that keeps growing."""
    out = []
    at = lambda ts: f"{round((ts - run_start_ms) / 1000)} s" if run_start_ms else "during the run"  # noqa: E731
    for s in series:
        points = s["points"]
        if s["metric"] in ("cpu_percent", "memory_percent") and len(points) >= 3:
            for i in range(len(points) - 2):
                if all(v >= 85 for _, v in points[i:i + 3]):
                    peak = max(v for _, v in points[i:])
                    what = "CPU" if s["metric"] == "cpu_percent" else "Memory"
                    out.append({"severity": "warning", "code": f"server_{s['metric']}",
                                "message": f"{what} on {s['monitor']} stayed above 85% from {at(points[i][0])} "
                                           f"(peak {peak:g}%). The server may be the bottleneck."})
                    break
        if s["metric"] == "memory_percent" and len(points) >= 6:
            values = [v for _, v in points]
            third = max(1, len(values) // 3)
            first, last = sum(values[:third]) / third, sum(values[-third:]) / third
            rising = sum(1 for a, b in zip(values, values[1:]) if b >= a) >= 0.7 * (len(values) - 1)
            if last - first >= 10 and rising:
                out.append({"severity": "warning", "code": "memory_growth",
                            "message": f"Memory on {s['monitor']} grew from about {first:.0f}% to {last:.0f}% and kept "
                                       "rising. In a long run this can mean a memory leak."})
    return out
