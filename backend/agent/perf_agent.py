#!/usr/bin/env python3
"""Server monitor agent for the load test console. Standard library only: copy this one file to a
server and run it there; the console polls it during test runs.

    python perf_agent.py --host 0.0.0.0 --port 9101 --token <a long random secret>

GET /metrics.json returns CPU, memory and disk use (Linux and Windows), load average and network
traffic (Linux, macOS where available). With --token, requests must send the header
X-Agent-Token: <secret>. It binds to 127.0.0.1 unless --host says otherwise.
"""
from __future__ import annotations

import argparse
import ctypes
import hmac
import json
import os
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1"


def _cpu_times() -> tuple[float, float] | None:
    """(idle, total) CPU time since boot, in any consistent unit."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/stat", encoding="ascii") as fh:
                fields = [float(x) for x in fh.readline().split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)     # idle + iowait
            return idle, sum(fields[:8])
        except (OSError, ValueError, IndexError):
            return None
    if os.name == "nt":
        class FILETIME(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        value = lambda ft: (ft.high << 32) | ft.low  # noqa: E731
        return float(value(idle)), float(value(kernel) + value(user))     # kernel time includes idle time
    return None


def _memory() -> tuple[float, float] | None:
    """(percent used, megabytes used)."""
    if sys.platform.startswith("linux"):
        try:
            info = {}
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    info[key] = float(rest.split()[0])
            total, available = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
            return round(100 * (1 - available / total), 1), round((total - available) / 1024, 1)
        except (OSError, KeyError, ValueError, ZeroDivisionError):
            return None
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        used = status.ullTotalPhys - status.ullAvailPhys
        return round(100 * used / status.ullTotalPhys, 1), round(used / 1024 / 1024, 1)
    return None


def _network() -> tuple[float, float] | None:
    """(bytes received, bytes sent) on all interfaces except loopback (Linux)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        rx = tx = 0.0
        with open("/proc/net/dev", encoding="ascii") as fh:
            for line in fh.readlines()[2:]:
                name, _, data = line.partition(":")
                if name.strip() == "lo":
                    continue
                fields = data.split()
                rx += float(fields[0])
                tx += float(fields[8])
        return rx, tx
    except (OSError, ValueError, IndexError):
        return None


class Collector:
    """Turns cumulative counters into rates between two samples."""

    def __init__(self, disk_path: str | None = None) -> None:
        self.disk_path = disk_path or (os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/")
        self._lock = threading.Lock()
        self._cpu = _cpu_times()
        self._net = _network()
        self._at = time.monotonic()

    def sample(self) -> dict[str, float | str | None]:
        with self._lock:
            now, cpu, net = time.monotonic(), _cpu_times(), _network()
            elapsed = max(now - self._at, 1e-6)
            out: dict[str, float | str | None] = {"host": socket.gethostname(), "time": time.time(), "version": VERSION,
                                                  "cpu_percent": None, "memory_percent": None, "memory_used_mb": None,
                                                  "disk_percent": None, "load1": None, "net_rx_kbps": None,
                                                  "net_tx_kbps": None}
            if cpu and self._cpu and cpu[1] > self._cpu[1]:
                busy = 1 - (cpu[0] - self._cpu[0]) / (cpu[1] - self._cpu[1])
                out["cpu_percent"] = round(max(0.0, min(100.0, 100 * busy)), 1)
            memory = _memory()
            if memory:
                out["memory_percent"], out["memory_used_mb"] = memory
            try:
                usage = shutil.disk_usage(self.disk_path)
                out["disk_percent"] = round(100 * usage.used / usage.total, 1)
            except OSError:
                pass
            if hasattr(os, "getloadavg"):
                try:
                    out["load1"] = round(os.getloadavg()[0], 2)
                except OSError:
                    pass
            if net and self._net:
                out["net_rx_kbps"] = round((net[0] - self._net[0]) / 1024 / elapsed, 1)
                out["net_tx_kbps"] = round((net[1] - self._net[1]) / 1024 / elapsed, 1)
            self._cpu, self._net, self._at = cpu or self._cpu, net or self._net, now
            return out


def make_server(host: str, port: int, token: str | None) -> ThreadingHTTPServer:
    collector = Collector()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"perf-agent/{VERSION}"

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            if token and not hmac.compare_digest((self.headers.get("X-Agent-Token") or "").encode(), token.encode()):
                self._send(401, {"error": "missing or wrong X-Agent-Token"})
            elif self.path.split("?")[0] == "/metrics.json":
                self._send(200, collector.sample())
            else:
                self._send(404, {"error": "use /metrics.json"})

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:   # keep the console quiet
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (0.0.0.0 for every interface)")
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--token", default=os.environ.get("PERF_AGENT_TOKEN"), help="secret the console must send")
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.token)
    print(f"perf agent listening on http://{args.host}:{args.port}/metrics.json"
          f"{' (token required)' if args.token else ' (no token: use --token on shared networks)'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
