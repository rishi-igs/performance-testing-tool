"""Command line client: submit a YAML config, wait for the result, set the exit code.

    python -m app.cli run ../config/sample.yaml --server http://127.0.0.1:8000
    python -m app.cli scenario c_1234abcd --server http://127.0.0.1:8000

Exit codes: 0 = passed, 1 = failed thresholds or SLAs, 2 = could not run the test.
If the server requires sign-in, set PERF_API_KEY to its API_KEY or to a personal
API token (Your account > API tokens in the dashboard).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import yaml


def _request(server: str, path: str, method: str = "GET", body: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if os.environ.get("PERF_API_KEY"):
        headers["X-API-Key"] = os.environ["PERF_API_KEY"]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"Server returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach {server}: {exc.reason}") from exc


def run(args: argparse.Namespace) -> int:
    with open(args.config, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    created = _request(args.server, "/tests", "POST", config)
    test_id = created["id"]
    print(f"Started {test_id}: {config.get('name')}")

    try:
        while True:
            status = _request(args.server, f"/tests/{test_id}/status")
            if status["status"] != "running":
                break
            m = status.get("metrics") or {}
            print(f"  running… {m.get('total_requests', 0)} requests, {m.get('error_rate_percent', 0)}% errors", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Interrupted, stopping the test…")
        _request(args.server, f"/tests/{test_id}/stop", "POST")
        return 2

    if status["status"] == "failed":
        print(f"Test failed to run: {status.get('error')}")
        return 2
    m = status["metrics"]
    rt = m["response_time_ms"] or {}
    print(f"\nResult: {m['verdict'].upper()}  ({status['status']})")
    print(f"  requests {m['total_requests']}, errors {m['error_rate_percent']}%, "
          f"p50 {rt.get('p50')} ms, p95 {rt.get('p95')} ms, p99 {rt.get('p99')} ms, {m['throughput_rps']} req/s")
    for w in m["warnings"]:
        print(f"  [{w['severity']}] {w['message']}")
    print(f"  Report: {args.server.rstrip('/')}/reports/{test_id}")
    return 0 if m["verdict"] == "pass" else 1


def run_scenario(args: argparse.Namespace) -> int:
    """Run a saved scenario; the exit code reflects its SLAs (a CI/CD gate)."""
    started = _request(args.server, f"/scenarios/{args.scenario}/run", "POST", {})
    test_id = started["id"]
    print(f"Started {test_id} for scenario {args.scenario}")
    for warning in started.get("warnings", []):
        print(f"  note: {warning}")
    try:
        while True:
            status = _request(args.server, f"/tests/{test_id}/status")
            if status["status"] != "running":
                break
            m = (status.get("metrics") or {}).get("requests") or {}
            print(f"  running… {m.get('total_requests', 0)} requests, {m.get('error_rate_percent', 0)}% errors", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Interrupted, stopping the test…")
        _request(args.server, f"/tests/{test_id}/stop", "POST")
        return 2
    if status["status"] == "failed":
        print(f"Test failed to run: {status.get('error')}")
        return 2
    m = status["metrics"]
    print(f"\nResult: {m['verdict'].upper()}  ({status['status']})")
    for t in m["transactions"]:
        print(f"  {t['display']:<30} {t['count']:>7} done  {t['failed']:>5} failed  avg {t['avg']:>8} ms  "
              f"p90 {t['p90']:>6} ms  {t['tps']:>7}/s")
    for rule in m["sla"]:
        print(f"  SLA {rule['status'].upper():<7} {rule['transaction']} {rule['metric']} {rule['actual']} (limit {rule['limit']})")
    for w in m["warnings"]:
        print(f"  [{w['severity']}] {w['message']}")
    print(f"  Report: {args.server.rstrip('/')}/reports/{test_id}")
    return 0 if m["verdict"] == "pass" else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="perf-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run a test from a YAML file and wait for the result")
    p.add_argument("config")
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--interval", type=float, default=3.0, help="seconds between progress checks")
    p.set_defaults(func=run)
    s = sub.add_parser("scenario", help="run a saved scenario by id and wait for its SLA verdict")
    s.add_argument("scenario")
    s.add_argument("--server", default="http://127.0.0.1:8000")
    s.add_argument("--interval", type=float, default=3.0, help="seconds between progress checks")
    s.set_defaults(func=run_scenario)
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
