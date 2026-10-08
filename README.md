# Performance Testing Tool

A management layer around Apache JMeter. You describe a test (URL, users, duration);
the tool generates the JMeter plan, runs JMeter headlessly, tracks the run, parses the
results into p50/p95/p99, throughput and error rate, and explains problems in plain language.

**Status.** The LoadRunner-style workflow is in place, on top of JMeter:

| LoadRunner | Here |
|---|---|
| VuGen | import a HAR or `.jmx`, group requests into business functions, automatic and manual correlation, parameters (CSV, unique, random, dates), text checks, think time and pacing, debug replay against the recording |
| Controller | scenarios: groups of users running scripts, ramp-up and ramp-down, rendezvous points, goal-oriented throughput, SLAs per business function |
| Load generators | distributed runs on `jmeter-server` machines |
| Monitors | server CPU and memory during runs (an agent, Prometheus exporters, or this machine) |
| Analysis | results per business function, graph overlays, run comparison, SLA report, HTML/PDF/Word export |
| Enterprise | user accounts and roles, projects, scheduled runs, trends with a baseline |

Quick tests (one URL, step-load profiles) and the CLI gate remain. See "Known limitations" for
what is not covered or not verified.

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
and "Test plan (.jmx)" downloads. Plans are checked in automated tests against a JMeter stand-in
and, with `REAL_JMETER_BIN` set, against real JMeter 5.6.3 (see "Tests"); distributed runs on
`jmeter-server` and the Word report opening in Word have not been checked yet.

**Sign-in.** A fresh install is open to anyone who can reach it, as before. To require sign-in,
open **Team** and create the first administrator; see "Team: accounts, projects, schedules and trends".

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

Set `PERF_API_KEY` if the server requires sign-in: either the server's `API_KEY`, or a personal
API token (Your account > API tokens), which acts with your role and projects.

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

**Recorded data.** The HAR file itself is not stored. Cookies and response bodies are dropped;
`Authorization`, API-key and CSRF headers are replaced with variables such as
`${authorization}` (fill them in JMeter's User Defined Variables until correlation arrives).
Request bodies are kept as recorded, so use test accounts when recording. An imported `.jmx`
is stored as uploaded so it can be rebuilt. Delete an import from its page when you are done.

| Method | Path | Purpose |
|---|---|---|
| POST | `/scripts/import-har?name=` | upload a HAR as the raw request body |
| POST | `/scripts/import-jmx?name=` | upload a `.jmx` as the raw request body |
| GET | `/scripts` | list imports |
| GET | `/scripts/{id}` | requests with keep/exclude reasons, business functions, rules |
| PUT | `/scripts/{id}/filter` | `{"changes": [{"index": 3, "include": false, "transaction": "Login"}], "rules": {...}}`; `null` returns a field to automatic |
| GET | `/scripts/{id}/export-jmx?users=1&loops=1&ramp_up_seconds=0` | download the grouped `.jmx` (load options apply to HAR imports) |
| DELETE | `/scripts/{id}` | delete an import |

```bash
curl -X POST --data-binary @recording.har -H "Content-Type: application/json" \
     "http://127.0.0.1:8000/scripts/import-har?name=Checkout"
```

## Script design and debug replay

Each imported script has the tabs LoadRunner users know from VuGen. Everything becomes ordinary
JMeter elements, so the exported plan opens and runs in the JMeter GUI.

| Tab | What it does | JMeter elements it adds |
|---|---|---|
| Correlation | Captures values the server issues (login tokens, CSRF tokens, new IDs, redirect codes, cookies copied into headers) and sends them back. **HAR imports are correlated automatically**: a dynamic-looking value a request sends that an earlier response contained gets an extractor and becomes `${variable}` everywhere it is sent. Add or edit rules by hand. | JSON Extractor (`$.token`), Boundary Extractor (left/right text, like `web_reg_save_param`), Regular Expression Extractor (body or headers) |
| Parameters | Test data per user: CSV data files (each column a variable; shared, per thread group or per user; recycle, stop or continue at the end), lists (sequential or random), random numbers, unique numbers, UUIDs, dates (Java format, offset in days, or Unix time) and random text. "Replace recorded text" turns a recorded value into `${variable}` everywhere. | CSV Data Set Config, Counter, Random Variable, or a function at each use (`__UUID`, `__timeShift`, `__RandomString`, `__Random`) |
| Checks | What a correct response looks like, on one request or all: text present or absent, regex, JSON field exists or equals, header text, response time, body size. Suggestions come from the recording (page titles, `success`/`status` fields). | Response Assertion, JSON Assertion, Duration Assertion, Size Assertion |
| Run-time settings | Think time between business functions (as recorded × factor with a cap, none, fixed, random), pacing (start every N s, or wait fixed/random after each iteration), what to do on an error, a new visitor each iteration, timeout, recorded status checks. | Flow Control Action + Constant/Uniform Random Timer (outside Transaction Controllers, so not counted as response time), Constant Throughput Timer (pacing), Thread Group error action |
| Debug replay | Runs the script once with one user (no think time or pacing) and compares every request with the recording: status codes, unresolved `${variables}`, extractors that found nothing, failed checks, connection errors. The first failure is highlighted with its likely cause and fix, and each request's full request and response can be opened (credentials masked unless you choose to show them). The last 10 replays are kept. | Debug Sampler after each request that feeds a correlation |

The page also lists every variable that is used but never set, with one click to correlate it or
make it a parameter. A script with data files downloads as a `.zip` (plan plus `data/*.csv`).

### What is automatic

- **HAR imports** are correlated during the import: a HAR holds the server's responses, so every
  dynamic value a request sends is traced to the response that issued it.
- **JMX imports** (and HARs saved without response bodies) hold no responses. On the Correlation
  tab, **Correlate automatically** replays the script with one user, as VuGen does when it scans
  after a replay. Each value the script still sends as recorded and that looks dynamic is matched
  with what the server sent in earlier responses:
  - **by value:** the recorded value came back unchanged;
  - **by name:** the same form input, meta tag, JSON key, link parameter, header or cookie holds
    a fresh value of the same shape. For example, `X-CSRF-Token` matches `<meta name="csrf-token">`,
    `X-XSRF-TOKEN` matches the `XSRF-TOKEN` cookie, a Bearer token matches `access_token`, and
    `/orders/<id>` matches `{"id": ...}`.

  Rules are added and the script replays again until nothing new turns up (at most 4 replays);
  the result names the rules, says whether the last replay passed, and links to it. Requests
  keep going after a failure during these replays, so every response is seen.
- **At import, with no replay:**
  - **A recorded login** (a user-name field and a password field in one request) becomes
    `users.csv`, holding the recorded account, with `${username}` and `${password}` in the
    requests. Add a row per test account. This is skipped when either value also appears
    elsewhere in the script (a user name `admin` in an `/admin/` path, say).
  - **UUIDs and timestamps** that the browser generated and that one request sends get a new
    value on each use.
  - **Search terms** and other typed input are suggested as list parameters.
  - The Parameters tab says what was set up.

Short numbers (under 8 digits) and plain words are not treated as dynamic. Correlate those by
hand if the server issues them.

| Method | Path | Purpose |
|---|---|---|
| POST | `/scripts/{id}/autocorrelate` | start "Correlate automatically" (testers; 409 while one runs) |
| GET | `/scripts/{id}/autocorrelate` | its progress, or the last result (`idle` if never run) |
| PUT | `/scripts/{id}/design` | replace `correlations`, `parameters`, `data_files`, `replacements`, `checks` or `settings` (parts left out are kept) |
| POST | `/scripts/{id}/files?name=users.csv&header=true` | upload a CSV data file (raw body, up to 10 MB) |
| DELETE | `/scripts/{id}/files/{name}` | remove a data file |
| POST | `/scripts/{id}/debug-run` | start a debug replay |
| GET | `/scripts/{id}/debug-runs` | the last replays |
| GET | `/debug-runs/{run_id}` | per-request comparison, diagnosis, captured values |
| GET | `/debug-runs/{run_id}/samples/{n}?reveal=false` | full request and response of request #n |

All hosts a script calls pass the same checks as a quick test (`ALLOW_PRIVATE_TARGETS`,
`ALLOWED_HOSTS`) before a replay starts.

## Scenarios (the Controller)

A scenario decides who runs what, like a LoadRunner Controller scenario:

- **Groups**: each runs one script with its own users (or a share of a total, percentage mode),
  its own schedule or the scenario's, and optionally no think time.
- **Schedule**: start delay, ramp-up, steady load, ramp-down, or a number of iterations per user.
  Ramp-down uses up to 10 staggered thread groups, so no JMeter plugins are needed.
- **Rendezvous points**: a share of a group's users waits before a business function, then acts at
  once (Synchronizing Timer, with a timeout).
- **Goal-oriented**: hold a number of business functions or requests per second with up to N users
  (Precise Throughput Timer); the result says whether the goal was reached once all users ran.
- **SLAs**: 90th/95th/99th percentile, average, maximum, error rate or minimum throughput, per
  business function or for every one. Any missed SLA fails the run.
- **Network speed**: per-user bandwidth emulation (JMeter's `httpclient.socket.http(s).cps`).

Each group's script keeps its own defaults, cookie and header managers and data files inside its
thread groups, so scripts do not affect each other. Transaction Controllers do not count timer
waits as response time (`includeTimers=false`, as in LoadRunner).

The run view is live: running users per group, 90th percentile and throughput per business
function, LoadRunner's transaction summary (count, passed, failed, min, average, max, 90%, 95%,
per second), SLA results, the goal, and per-group errors.

| Method | Path | Purpose |
|---|---|---|
| POST / GET | `/scenarios` | create / list scenarios |
| GET / PUT / DELETE | `/scenarios/{id}` | read, replace, delete |
| GET | `/scenarios/{id}/preview` | planned running users over time, per group, and plan warnings |
| GET | `/scenarios/{id}/export-jmx` | the combined plan (`.zip` with data files when needed) |
| POST | `/scenarios/{id}/run` | start a run (a test with `kind: scenario`) |
| GET | `/tests?kind=scenario&scenario_id=` | its runs; `/tests/{id}/status` and `/timeline` work as for quick tests |

From a pipeline, `python -m app.cli scenario <scenario id>` waits for the run and exits 0 when
every SLA is met, 1 when one is missed, 2 when the run could not start or failed.

JMeter logs a harmless `Existing CookieManager ... superseded` warning for requests whose
response sets a cookie, from the second iteration on; it does not affect cookies.

## Analysis, comparison and reports

Every finished run (quick test or scenario) has an **Analysis** section:

- **Graph builder**: tick any series to overlay them, LoadRunner Analysis style: running users
  (overall and per group), hits per second, throughput, errors per second, HTTP codes per
  second, response time of all requests, and per business function its average, 90th percentile,
  throughput and failures (plus per request and, with monitors, server metrics). The first unit
  uses the left axis, the others the right.
- **Error statistics**: failures grouped by code and message, with how often, when they started
  and which requests.
- **Compare with another run**: per business function, the count, 90th percentile, average,
  error rate and throughput of both runs, with the change in percent. A business function is
  flagged slower when its 90th percentile grew by more than 10% and 50 ms, or with more errors
  when its error rate grew by more than one point. Users, hits and response time are overlaid
  by elapsed time.
- **Reports**: a self-contained HTML report (inline SVG charts, no scripts; print it to PDF), a
  PDF with vector charts, and a Word document with the same tables (charts are in the PDF and
  HTML versions). All are generated without extra packages.

| Method | Path | Purpose |
|---|---|---|
| GET | `/tests/{id}/analysis?bucket_seconds=5` | every graph series and the error statistics |
| GET | `/tests/{a}/compare/{b}` | run `b` against baseline `a` |
| GET | `/reports/{id}/summary.html` (`?download=true`), `summary.pdf`, `summary.docx` | the run report |

## Load generators and server monitors

**Load generators** are machines running `jmeter-server` (JMeter's distributed mode). Add them
under Infrastructure, then pick them in a scenario: the run passes `-R host:port,...` to JMeter and
splits each group's users evenly across them (JMeter runs the whole plan on every engine). Before
a run the console checks it can reach each one.

- JMeter's RMI channel expects a keystore. On a trusted network, start every `jmeter-server` with
  `-Jserver.rmi.ssl.disable=true` and set `JMETER_RMI_SSL_DISABLE=true` for the console.
- Data files are not sent to the engines. Copy them (the scenario's "Download plan" zip holds them)
  to the same folder on every generator and set `GENERATOR_DATA_DIR` to it; the plan reads
  `${__P(perf.data_dir)}/data/...`.

**Server monitors** read metrics while a scenario runs; pick them in the scenario. Samples are
stored with the run (`monitors.csv`), drawn in the analysis graphs and reports, and produce
findings such as "CPU on db-01 stayed above 85%" or memory that keeps growing (a possible leak).

| Kind | Reads | Setup |
|---|---|---|
| Agent | CPU, memory, disk (Linux, Windows), load and network (Linux) | copy `backend/agent/perf_agent.py` (or download it from the Infrastructure page) and run `python perf_agent.py --host 0.0.0.0 --port 9101 --token <secret>` |
| Prometheus | node_exporter or windows_exporter CPU and memory presets, or any metric as a gauge or rate | the exporter's URL, e.g. `http://app-01:9100/metrics` |
| This machine | the console's own machine | nothing |

Monitor URLs are fetched by the console, so they follow the same target rules as tests
(`ALLOWED_HOSTS` / `ALLOW_PRIVATE_TARGETS`). Agent tokens are never returned by the API.

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/generators` | list / add load generators |
| PUT / DELETE | `/generators/{id}` | change / remove |
| POST | `/generators/{id}/check` | can the console connect? |
| GET / POST | `/monitors` | list / add monitors |
| PUT / DELETE | `/monitors/{id}` | change / remove |
| POST | `/monitors/{id}/check` | read it now and show the values |
| GET | `/agent/perf_agent.py` | download the agent |

## Team: accounts, projects, schedules and trends

**Accounts and roles.** Until the first account exists the console is open (anyone who can reach
it is an administrator). Open **Team** and create the first administrator; from then on everyone
signs in. Roles:

| Role | Can |
|---|---|
| Viewer | see scripts, scenarios, runs, analysis and reports |
| Tester | also import and edit scripts, build and run scenarios and tests, schedule runs, pick baselines, show recorded credentials in debug replays |
| Administrator | also manage users, projects, load generators and monitors, and see every project |

- Passwords: at least 10 characters, stored as PBKDF2-SHA256 hashes (600,000 rounds).
- Sessions last 12 hours in an HttpOnly, SameSite=Strict cookie (`Secure` on HTTPS, or always with
  `SECURE_COOKIES=true` behind a TLS proxy). Writes made with a session also need the header
  `X-Requested-With: console`, which the dashboard sends and other sites cannot.
- After 5 failed sign-ins within 15 minutes, that user name and that address must wait.
- Disabling a user or resetting their password signs them out everywhere. The last enabled
  administrator cannot be demoted, disabled or deleted.
- **API tokens** (Your account > API tokens) are for the CLI and pipelines: send `X-API-Key: pt_...`.
  A token acts as its owner, with the owner's current role and projects; it is shown once and
  only its hash is stored. The old `API_KEY` setting still works and acts as an administrator.

**Projects** keep teams' work apart: each script, scenario, run and schedule belongs to one.
Administrators see all of them; everyone else sees the projects they are members of (add people in
the Users table). Pick the project in the rail; new work goes into it. A scenario can only use
scripts of its own project. Existing work is in the **Default** project, which cannot be deleted.
Other projects can be deleted while they are empty; as runs cannot be deleted yet, that means
before their first run.

**Scheduled runs** (on a saved scenario): once, every day, or on chosen weekdays, at a local time.
A background check every `SCHEDULER_INTERVAL_SECONDS` starts what is due; each schedule is claimed
in the database first, so a run never starts twice. A run that fell due more than 10 minutes ago
(the server was down) is **skipped, not started late**, and the schedule says so. Scheduled runs
pass the same limits and target checks as runs you start; problems are shown on the schedule.
Times follow the browser's time zone including daylight saving when the server has a time-zone
database: Linux has one; on Windows run `pip install tzdata`. Without it, the UTC offset at the
time the schedule was saved is used, and the schedule is marked "Fixed UTC offset".

**Trends** (on a saved scenario) list every finished run with its numbers and a chart of each
business function's 90th percentile. Choose a run as the **baseline** and the latest run is
compared with it, using the same rule as run comparison (90th percentile more than 10% and 50 ms
slower, or error rate up by more than one point).

| Method | Path | Purpose |
|---|---|---|
| GET | `/auth/me` | who is signed in, their role and projects |
| POST | `/auth/setup` | create the first administrator (only while there are no accounts) |
| POST | `/auth/login`, `/auth/logout` | sign in and out (dashboard header required) |
| POST | `/auth/password` | change your password |
| GET / POST / DELETE | `/auth/tokens`, `/auth/tokens/{id}` | your API tokens |
| GET / POST, PUT / DELETE | `/users`, `/users/{id}` | user accounts (administrators) |
| GET / POST, PUT / DELETE | `/projects`, `/projects/{id}` | projects and their members (administrators) |
| GET / POST, PUT / DELETE | `/schedules`, `/schedules/{id}` | scheduled runs (`?scenario_id=`) |
| GET | `/scenarios/{id}/trends?limit=30` | runs over time and the latest run against the baseline |
| PUT | `/scenarios/{id}/baseline` | `{"run_id": "t_..."}` or `{"run_id": null}` |

Lists take `?project=<id>`, and so do the calls that create scripts, scenarios and tests.

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
| `API_KEY` | unset | a shared key that acts as an administrator (`X-API-Key`); also turns sign-in on |
| `SECURE_COOKIES` | `false` | mark the session cookie `Secure` even when the console sees plain HTTP (behind a TLS proxy) |
| `SCHEDULER_INTERVAL_SECONDS` | 15 | how often scheduled runs are checked; `0` turns the scheduler off |
| `ALLOW_PRIVATE_TARGETS` | `false` | allow loopback/private-network targets |
| `ALLOWED_HOSTS` | empty | comma-separated hosts always allowed |
| `MAX_USERS` | 1000 | per-test cap |
| `MAX_DURATION_SECONDS` | 3600 | per-test cap (raise for soak tests) |
| `MAX_CONCURRENT_TESTS` | 2 | simultaneous runs |
| `MAX_UPLOAD_MB` | 50 | largest HAR or `.jmx` you can import |
| `JMETER_RMI_SSL_DISABLE` | `false` | distributed runs without JMeter's RMI keystore (trusted networks only) |
| `GENERATOR_DATA_DIR` | empty | folder holding the data files on each load generator |

## Security behaviour

- Target URLs are resolved and checked: link-local/cloud-metadata addresses are always refused;
  private and loopback addresses are refused unless you opt in.
- Header secrets are never stored in the database or returned by the API. The generated
  `test_plan.jmx` does contain them (JMeter needs them), so it is written with owner-only permissions.
- Starting and stopping tests, sign-ins, and changes to scripts, scenarios, schedules, users,
  projects and infrastructure are logged with the caller identity (`perf.audit` logger).
  Passwords and tokens are never logged.
- With no accounts and no `API_KEY` the tool is open. Only run it that way on a trusted machine.
- People see only the projects they are members of; items in other projects answer 404.
- Only test systems you own or are authorised to test.

## Tests

```bash
cd backend && python -m pytest
```

Two optional groups run when their tools are present:

```bash
# Against real Apache JMeter (correlation, automatic correlation of a .jmx, the recorded login as
# users.csv, parameters, checks, pacing and think time, rendezvous and ramp-down, goal throughput):
REAL_JMETER_BIN=/path/to/apache-jmeter-5.6.3/bin/jmeter python -m pytest tests/test_real_jmeter.py
# The dashboard end to end (needs Node.js): runs automatically when `node` is on PATH
python -m pytest tests/test_ui_smoke.py
```

Config validation, SSRF checks, plan generation (including XML escaping), result math
and warnings, end-to-end runs (start, live metrics, stop, failure detection, API key,
limits, restart recovery), HAR/JMX import (filter rules, grouping, secret removal,
regrouped export, upload limits), correlation, parameters and debug replay, scenarios, analysis
and reports, generators and monitors, and accounts, roles, projects, tokens, schedules and trends.
End-to-end tests use `tests/fake_jmeter.py`, a small stand-in that reads the generated plan and
sends real HTTP requests to `tests/sample_app.py`, so the pipeline can be tested without JMeter.

## Known limitations

- Distributed runs (`-R` to `jmeter-server`) are built and checked for reachability, but have not
  been run against real `jmeter-server` machines yet. Data files must be copied to the generators.
- The Word report has not been opened in Microsoft Word by an automated test; it holds no charts
  (the HTML and PDF reports do).
- Accounts are local to the tool: no single sign-on or LDAP.
- Without a time-zone database (Windows without `tzdata`), schedules use a fixed UTC offset.
- Imported scripts keep the recorded request bodies (which can hold test-account passwords),
  `.jmx` imports keep the original file, and debug replays keep full requests and responses in
  their run folder (the last 10 per script). None of this is encrypted at rest and, apart from
  old debug replays, nothing is deleted automatically: protect the data folder and delete scripts
  you no longer need (runs cannot be deleted from the console yet).
- Results are re-parsed from the JTL on each status call. Fine for runs of minutes to an hour or
  so; very long soak runs would need incremental parsing.
- If the server process is killed hard (not shut down), a running JMeter keeps going until its
  scheduled duration ends. The run is marked failed on the next start.
- DNS is checked at creation and again by JMeter, so a hostile DNS server could differ between the two.

## Layout

```
backend/app/   main.py, config.py, db.py, deps.py, cli.py
  models/      test_config.py, test_result.py, script.py, scenario.py, infra.py, account.py, schedule.py
  services/    jmeter_plan_builder.py, test_executor.py, result_analyzer.py, security.py, auth.py,
               har_parser.py, jmx_importer.py, xmlsafe.py, request_filter.py, correlation.py, autocorrelate.py,
               script_builder.py, debug_runner.py, scenario_builder.py, scenario_analyzer.py,
               analysis.py, report.py, report_pdf.py, report_docx.py, monitoring.py, scheduler.py
  routers/     tests.py, reports.py, scripts.py, debug.py, scenarios.py, schedules.py, infra.py,
               auth.py, users.py, projects.py
backend/agent/ perf_agent.py (server monitor agent, standard library only)
backend/tests/ unit + end-to-end tests, sample_app.py, fake_jmeter.py, recorder.py, recordings.py
frontend/      index.html and js/ (the dashboard, served by the backend); tests/smoke.js
config/        sample.yaml, step-load.yaml
scripts/       dev.sh
```
