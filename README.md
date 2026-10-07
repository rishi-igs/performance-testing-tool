# Performance Testing Tool

A management layer around Apache JMeter. You describe a test (URL, users, duration);
the tool generates the JMeter plan, runs JMeter headlessly, tracks the run, parses the
results into p50/p95/p99, throughput and error rate, and explains problems in plain language.

**Status: Phase 2 in progress.** One request per test, fixed-user tests and step-load profiles,
API + dashboard + CLI. HAR import, other traffic profiles, CI baselines, etc. are still to come
(see "What's next").

## Quick start

Requirements: Python 3.11+, Java 17+, [Apache JMeter 5.6+](https://jmeter.apache.org/download_jmeter.cgi).

```bash
cd backend
pip install -r requirements.txt

export JMETER_BIN=/path/to/apache-jmeter-5.6.3/bin/jmeter   # Windows: set JMETER_BIN=...\bin\jmeter.bat
export ALLOW_PRIVATE_TARGETS=true                            # only for testing localhost / LAN targets

# terminal 1: a local target to test against
python -m uvicorn tests.sample_app:app --port 9000
# terminal 2: the tool
python -m uvicorn app.main:app --port 8000
```

Open <http://127.0.0.1:8000>, enter `http://127.0.0.1:9000/slow?ms=120`, and start a test.
(`scripts/dev.sh` starts both for you on macOS/Linux.)

**First thing to do on your machine:** run one 10-second test and open the "JMeter HTML report"
and "Test plan (.jmx)" downloads. The generated plan was verified structurally and against a
JMeter stand-in in automated tests, but not against a real JMeter installation, so confirm it
opens in the JMeter GUI and the report renders.

### Docker

```bash
docker build -t perf-tool .
docker run -p 8000:8000 -v perf-data:/data -e API_KEY=change-me perf-tool
```

### From a pipeline (CLI)

```bash
cd backend
python -m app.cli run ../config/sample.yaml --server http://127.0.0.1:8000
echo $?     # 0 passed, 1 failed thresholds, 2 could not run
```

Set `PERF_API_KEY` if the server has `API_KEY` set.

## Configuration of a test

An optional `profile` configures an increasing step load. Each step adds users at its
`time_seconds` offset; added users stay active until the test ends. The final step must
start before `duration_seconds`, match `users`, and `ramp_up_seconds` must be zero.
Omit `profile` to retain the fixed-user load behavior.

```yaml
users: 100                 # peak users; must match the final step
duration_seconds: 90
ramp_up_seconds: 0
profile:
  - time_seconds: 0
    users: 10
  - time_seconds: 30
    users: 50
  - time_seconds: 60
    users: 100
```

| Field | Default | Notes |
|---|---|---|
| `name` | required | |
| `target_url` | required | http/https; no credentials in the URL |
| `method` | `GET` | GET, POST, PUT, PATCH, DELETE, HEAD |
| `headers` | `{}` | secret headers are masked in all API responses |
| `body` | none | raw request body |
| `users` | 10 | virtual users |
| `duration_seconds` | 60 | total run time, including ramp-up |
| `ramp_up_seconds` | 0 | users start evenly over this period |
| `profile` | none | optional increasing step-load targets (`time_seconds`, `users`); final target must equal `users` |
| `think_time_ms` | 0 | pause between requests per user |
| `timeout_ms` | 30000 | connect and response timeout |
| `expected_status_codes` | `[200]` | anything else counts as a failure |
| `thresholds.max_error_rate_percent` | 1.0 | exceeding it fails the run |
| `thresholds.max_p95_ms` | 1000 | `null` disables |

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/tests` | create and start a test, returns a unique `id` |
| GET | `/tests` | list tests |
| GET | `/tests/{id}` | config, state, summary |
| GET | `/tests/{id}/status` | state plus live metrics |
| GET | `/tests/{id}/timeline` | per-interval series for charts |
| POST | `/tests/{id}/stop` | stop a running test (partial results are kept) |
| GET | `/reports/{id}` | JMeter HTML report |
| GET | `/reports/{id}/metrics.json` | export metrics |
| GET | `/reports/{id}/results.csv` | raw JTL results |
| GET | `/reports/{id}/plan.jmx` | the generated plan (open it in JMeter) |
| GET | `/reports/{id}/jmeter.log` | JMeter log |

Interactive docs: <http://127.0.0.1:8000/docs>.

## Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `JMETER_BIN` | `jmeter` | path to the JMeter launcher |
| `PERF_DATA_DIR` | `data` | database and run folders |
| `API_KEY` | unset | when set, every call needs `X-API-Key` (the dashboard asks once) |
| `ALLOW_PRIVATE_TARGETS` | `false` | allow loopback/private-network targets |
| `ALLOWED_HOSTS` | empty | comma-separated hosts always allowed |
| `MAX_USERS` | 1000 | per-test cap |
| `MAX_DURATION_SECONDS` | 3600 | per-test cap (raise for soak tests) |
| `MAX_CONCURRENT_TESTS` | 2 | simultaneous runs |

## Security behaviour

- Target URLs are resolved and checked: link-local/cloud-metadata addresses are always refused;
  private and loopback addresses are refused unless you opt in.
- Header secrets are never stored in the database or returned by the API. The generated
  `test_plan.jmx` does contain them (JMeter needs them), so it is written with owner-only permissions.
- Starting and stopping tests is logged with the caller identity (`perf.audit` logger).
- Without `API_KEY` the tool is open. Only run it that way on a trusted machine.
- Only test systems you own or are authorised to test.

## Tests

```bash
cd backend && python -m pytest
```

39 tests: config validation, SSRF checks, plan generation (including XML escaping), result math
and warnings, and end-to-end runs (start, live metrics, stop, failure detection, API key,
limits, restart recovery). End-to-end tests use `tests/fake_jmeter.py`, a small stand-in that
reads the generated plan and sends real HTTP requests to `tests/sample_app.py`. It exists only so
the pipeline can be tested without JMeter installed.

## Known limitations (Phase 1)

- Results are re-parsed from the JTL on each status call. Fine for minutes-long tests; very long
  soak runs need incremental parsing (Phase 4/5).
- Warnings are inferred from client-side results only. Causes such as pool exhaustion are
  suggestions, not diagnoses, until infrastructure metrics arrive (Phase 6).
- If the server process is killed hard (not shut down), a running JMeter keeps going until its
  scheduled duration ends. The run is marked failed on the next start.
- One request per test, no authentication helpers beyond headers.
- DNS is checked at creation and again by JMeter, so a hostile DNS server could differ between the two.

## What's next (roadmap)

| Phase | Adds |
|---|---|
| 2 | Step-load profiles (implemented); ramp, spike, soak, and multi-phase profiles |
| 3 | HAR import: filtering, correlation, parameterization, validator, assertions, debug runs |
| 4 | Run comparison and live-chart polish |
| 5-9 | Analysis summaries, infrastructure metrics, CI baselines, distributed runs, browser tests |

## Layout

```
backend/app/   main.py, config.py, db.py, deps.py, cli.py
  models/      test_config.py, test_result.py
  services/    jmeter_plan_builder.py, test_executor.py, result_analyzer.py, security.py
  routers/     tests.py, reports.py
backend/tests/ unit + end-to-end tests, sample_app.py, fake_jmeter.py
frontend/      index.html (dashboard, served by the backend)
config/        sample.yaml
scripts/       dev.sh
```
