# Performance Testing Tool

A management layer around Apache JMeter. You describe a test (URL, users, duration);
the tool generates the JMeter plan, runs JMeter headlessly, tracks the run, parses the
results into p50/p95/p99, throughput and error rate, and explains problems in plain language.

**Status: Phase 2 traffic profiles and Phase 3A–3H implementation are in place.** The tool supports HAR/JMX import, request
filtering and grouping, JSON correlation, CSV parameterization, a HAR comparison validator,
scoped assertion authoring, and bounded debug runs with sample viewing, diagnosis, and reruns.
Phase 3's exit criterion still needs a real authenticated multi-step browser recording to pass
end-to-end against an authorized target. Full-load execution of imported recordings, other traffic
stress/spike profile scheduling, CI baselines, and later roadmap features are still to come.

## Quick start

Requirements: Python 3.11+, Java 17+, [Apache JMeter 5.6+](https://jmeter.apache.org/download_jmeter.cgi).

### Run every automated check with one command (Windows)

From the repository root, run:

```bat
npm run test
```

This command starts the local sample target and dashboard, checks both health endpoints and
the dashboard page, compiles the Python sources, runs the complete backend test suite, checks
the dashboard JavaScript syntax, runs the Playwright browser workflow, and stops the temporary
services when finished. Set
`JMETER_BIN` first if JMeter is not installed at `C:\apache-jmeter-5.6.3\bin\jmeter.bat`.
The same runner can also be called directly with `scripts\test-all.cmd` or
`.\scripts\test-all.ps1`.

The first setup requires browser-test dependencies:

```bat
npm install
npx playwright install chromium
```

The browser test opens the dashboard in Chromium, imports
`manual-test-data\phase3-demo.har`, validates the generated plan, starts a debug run, and
checks that both requests pass.

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
Use `--baseline <completed-test-id>` to fail the command when the new run regresses
error rate, latency, or throughput against a previous run.

## Import a recording (HAR or JMX)

Use **Import a recording** in the dashboard, or the API. The tool lists every captured request,
excludes the unwanted ones, groups the rest into business functions and exports a `.jmx` you
can open in JMeter. It accepts:

- a **HAR** file from the browser (DevTools > Network > right-click > *Save all as HAR*);
- a JMeter **.jmx**, for example one made with JMeter's HTTP(S) Test Script Recorder.

**Filtering.** On by default and editable per import (*Filter and grouping rules*):
static files by extension and content type, analytics/ads/chat-widget domains, `OPTIONS`
preflights, browser-internal and cancelled requests, and samplers disabled in an imported
`.jmx`. *Keep API calls only* also drops page navigations (HAR only; a JMX has no response types).
Excluded requests stay in the list with the reason and can be kept again.

**Business functions.** Each kept request gets a name, in this order of priority:

1. your own edit (rename a group or a single request in the table);
2. the first matching rule, `Name = URL pattern`, for example `Login = */auth/*`;
3. automatic: HAR imports start a new function on each page navigation or after 5 seconds
   without requests (JMeter's recorder default; configurable), named after the first API call
   or the page title. JMX imports use the Transaction/Simple Controller the sampler was in.

Consecutive kept requests with the same name become one Transaction Controller, numbered in
run order (`01 Login`, `02 Search`, ...), so JMeter reports results per business step.
Changing the rules keeps your manual edits.

**The exported plan** follows JMeter's element types:

| HAR import | JMX import |
|---|---|
| Test Plan with User Defined Variables for removed credentials | Your Test Plan, unchanged |
| HTTP Request Defaults (timeouts), HTTP Cookie Manager, a Header Manager for headers every request shares | Your plan- and thread-group-level elements, unchanged |
| One Thread Group (users, loops and ramp-up chosen at export) | Your thread groups and their settings |
| Transaction Controller per business function | Transaction Controller per business function |
| HTTP Request per kept request, redirects replayed as recorded, own Header Manager | Your samplers with all their children (extractors, assertions, timers, headers) |
| Response Assertion on the recorded status code ("Ignore status" on, so a recorded 404 passes) | Your assertions |
| View Results Tree, disabled | Your listeners |

When a `.jmx` is imported, Transaction, Simple and Recording Controllers are replaced by the
new grouping, and elements inside them move with their requests. Other logic controllers
(If, Loop, While, ...) are kept as one block so their behaviour does not change.

**Recorded data and correlation.** The HAR file itself and response bodies are not stored.
Cookies are dropped; `Authorization`, API-key and CSRF headers are replaced with variables.
For JSON responses, the importer detects sufficiently long or token/session/id-like values that
appear in later request URLs, headers or bodies. It saves only each JSONPath, source request,
target requests and variable name, substitutes `${variable}` into those requests, and adds a
JMeter JSON Extractor to the source sampler. Review or edit the variable, source request,
JSONPath and enabled state under **Correlations** before exporting. This initial correlation
pass does not yet detect values in non-JSON responses or correlate cookie values; JMeter's
Cookie Manager handles server-set cookies. Request bodies can still contain other recorded
personal or test data, so use sanitized recordings and test accounts. An imported `.jmx` is
stored as uploaded so it can be rebuilt. Delete an import from its page when you are done.

**CSV parameterization (HAR imports).** Upload a UTF-8 CSV to validate its unique column names
and row count, then map selected request values to JMeter variables and CSV columns. For example,
mapping `recorded-user` to the `username` column replaces that literal in the request with
`${username}`. The sample value is discarded after substitution. CSV contents are validated in
memory and never stored by the server; keep the original file in your browser and download it as
`parameters.csv` alongside the exported plan. Alternatively, run JMeter with
`-Jperf.data.file=<path-to-csv>`. Configure recycling and CSV sharing in the import review. When
recycling is disabled, the UI warns if the CSV has fewer rows than the export's user count.

**HAR comparison validator (initial).** Select **Validate generated plan** in an imported HAR
review to compare each kept request with its generated JMeter sampler for endpoint, method,
headers, body, recorded response-code assertion, and order. It also reports disabled or missing
correlation sources, unknown JMeter variables, and likely unparameterized IDs or token-like
values. Query values and request-body/header contents are not shown in the report. This checks
the generated plan structure; it does not execute requests against the target application.

**Assertions.** The assertion editor supports status codes (`200` or `2xx`), response-text
contains/not-contains/equals/regex, JSONPath, response headers, duration limits, and response-size
ranges. Rules can apply globally, to a transaction, or to a request. HAR imports suggest the
recorded status code and selected stable JSON success fields. Debug summaries count failed
assertion checks separately from failed samples. Review the suggestions and saved rules before
exporting or debugging.

**Debug runs (HAR imports).** Start a sequential JMeter run with 1–5 users and 1–5 iterations.
Debug runs are separate from performance-test metrics and use the same target security policy
and concurrency limit. The viewer shows each request/response, timing, assertions, and masked
correlation metadata; it supports failed-only filtering, body search, rule-based diagnosis, history,
and reruns. For parameterized scripts, send the validated CSV again as the raw request body for
each run and rerun. Raw JTL and JMeter-log downloads require the configured API key but may contain
unmasked request/response data; treat them as sensitive artifacts. Debug results currently have no
retention policy or at-rest encryption.

| Method | Path | Purpose |
|---|---|---|
| POST | `/scripts/import-har?name=` | upload a HAR as the raw request body |
| POST | `/scripts/import-jmx?name=` | upload a `.jmx` as the raw request body |
| GET | `/scripts` | list imports |
| GET | `/scripts/{id}` | requests with keep/exclude reasons, business functions, rules |
| PUT | `/scripts/{id}/filter` | `{"changes": [{"index": 3, "include": false, "transaction": "Login"}], "rules": {...}}`; `null` returns a field to automatic |
| PUT | `/scripts/{id}/correlations` | save edited JSONPath rules and variable names |
| PUT | `/scripts/{id}/parameters/csv` | validate CSV headers and row count; file contents are not retained |
| PUT | `/scripts/{id}/parameters` | replace recorded values with CSV-backed JMeter variables |
| POST | `/scripts/{id}/validate` | compare kept HAR requests with the generated plan and report unresolved values |
| PUT | `/scripts/{id}/assertions` | save scoped assertion rules |
| GET | `/scripts/{id}/export-jmx?users=1&loops=1&ramp_up_seconds=0` | download the grouped `.jmx` (load options apply to HAR imports) |
| POST | `/debug-runs/scripts/{id}?users=1&loops=1` | start a bounded debug run; send the CSV as the raw body when parameterized |
| GET | `/debug-runs/scripts/{id}` | list debug-run history |
| GET | `/debug-runs/{run_id}` | read debug-run status and summary |
| POST | `/debug-runs/{run_id}/rerun` | rerun with the same user/iteration limits; send CSV when parameterized |
| GET | `/debug-runs/{run_id}/samples?failed_only=true&search=` | list/search debug samples |
| GET | `/debug-runs/{run_id}/samples/{sample_index}` | read one sanitized sample |
| GET | `/debug-runs/{run_id}/diagnosis` | get rule-based failure suggestions |
| GET | `/debug-runs/{run_id}/results.jtl` | download raw debug JTL (sensitive) |
| GET | `/debug-runs/{run_id}/jmeter.log` | download JMeter log |
| DELETE | `/scripts/{id}` | delete an import |

```bash
curl -X POST --data-binary @recording.har -H "Content-Type: application/json" \
     "http://127.0.0.1:8000/scripts/import-har?name=Checkout"
```

Imported JMX files can be edited and exported. The current in-tool debug runner is for HAR imports;
to execute an imported JMX, open it in JMeter. Full-load execution of imported recordings is not
implemented yet.

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
| GET | `/tests/{id}/compare?baseline_id=<id>` | compare completed metrics with a baseline run |
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
| `MAX_UPLOAD_MB` | 50 | largest HAR or `.jmx` you can import |

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

Config validation, SSRF checks, plan generation (including XML escaping), result math
and warnings, end-to-end runs (start, live metrics, stop, failure detection, API key,
limits, restart recovery), and HAR/JMX import (filter rules, grouping, secret removal,
regrouped export, upload limits). End-to-end tests use `tests/fake_jmeter.py`, a small stand-in that
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
| 3 | HAR/JMX import, filtering, grouping, JSON correlation, CSV parameterization, HAR validator, assertion builder, and bounded HAR debug workflow (implemented; real multi-step exit criterion remains to be verified); full-load execution of imported recordings |
| 4 | Run comparison and live-chart polish |
| 5-9 | Analysis summaries, infrastructure metrics, CI baselines, distributed runs, browser tests |

## Layout

```
backend/app/   main.py, config.py, db.py, deps.py, cli.py
  models/      test_config.py, test_result.py, script.py
  services/    jmeter_plan_builder.py, test_executor.py, result_analyzer.py, security.py,
               har_parser.py, jmx_importer.py, request_filter.py
  routers/     tests.py, reports.py, scripts.py
backend/tests/ unit + end-to-end tests, sample_app.py, fake_jmeter.py, recordings.py
frontend/      index.html (dashboard, served by the backend)
config/        sample.yaml
scripts/       dev.sh
```
