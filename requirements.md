# Performance Testing Tool: Complete Requirements and Implementation Guide

*Combined version: original requirements plus the script authoring and debug workflow.*

---

## 1. Purpose

Develop a tool that automates performance testing, reduces manual effort, and helps engineers identify application bottlenecks before deployment. The tool should offer a simple workflow for creating, executing, monitoring, analyzing, and reporting performance tests.

The application should act as a smart management layer around Apache JMeter. JMeter should perform the actual load generation, while the application should handle configuration, orchestration, script preparation, correlation, dashboards, reporting, and CI/CD integration.

## 2. Problem Statement

Engineers currently spend significant time creating scripts, cleaning up recorded requests, configuring dynamic values, adding assertions, debugging failed scripts, running tests, reading large result files, analyzing logs, and repeating tests after fixes. The tool should automate these activities and provide clear performance results.

## 3. User Story

The tool should behave like a smart robot helper in a toy store. Instead of hiring thousands of real users, it creates virtual users that send requests to the application. It watches the system, detects degradation or failure, and reports the exact problem before real users arrive.

## 4. Expected User Experience

A user should be able to:

1. Enter the target application URL.
2. Upload a HAR file recorded from the browser and review the captured requests.
3. Filter out static and unwanted requests.
4. Review correlation suggestions and parameterize input data.
5. Validate the generated script against the original recording.
6. Add assertions.
7. Run a debug test and inspect each request and response.
8. Fix failures and re-run until the script passes.
9. Select a performance-testing type and traffic profile.
10. Configure users, duration, request method, headers, payload, and authentication.
11. Start or stop a test.
12. View live results.
13. Review charts and reports.
14. Export metrics and the generated `.jmx` script.
15. Integrate tests into CI/CD pipelines.

## 5. Supported Test Types

| Type | Objective | Example |
| --- | --- | --- |
| Load Testing | Evaluate normal and expected peak load | 100 users for 10 minutes |
| Stress Testing | Find limits and degradation | Increase users until errors occur |
| Spike Testing | Test sudden traffic increases | Increase from 5 to 500 users instantly |
| Soak or Endurance Testing | Check long-term stability | Keep moderate load for 24 to 72 hours |
| Volume Testing | Test large data sets or bulk operations | Normal users with large database payloads |
| Scalability Testing | Validate resource expansion | Increase users while adding server instances |
| Breakpoint Testing | Test complete application failure | Increase load until the system fails |

## 6. Traffic Profiles

The tool should allow the user to control the number of virtual users over time. Supported profiles include:

- Constant load
- Ramp-up load
- Step load
- Sudden spike
- Wave load
- Long flat load
- Gradual increase until failure
- Load reduction
- Pauses
- Multiple test phases

Example spike configuration:

```yaml
test_type: spike
target_url: https://api.example.com/checkout
profile:
  - time: 0s
    users: 5
  - time: 10s
    users: 500
  - time: 30s
    users: 5
```

Example stress configuration:

```yaml
test_type: stress
target_url: https://api.example.com/checkout
profile:
  - time: 0s
    users: 10
  - time: 30s
    users: 100
  - time: 60s
    users: 250
  - time: 90s
    users: 500
```

Example soak configuration:

```yaml
test_type: soak
target_url: https://api.example.com/products
profile:
  - time: 0s
    users: 50
  - time: 24h
    users: 50
```

## 7. Core Functional Requirements

### Test Configuration

The tool should allow configuration of the test name, target URL, request method, path, headers, body, authentication, test type, traffic profile, duration, virtual-user count, ramp-up and ramp-down periods, think time, loop count, timeout, expected status codes, and assertions.

### Test Execution

The tool should generate a JMeter test plan, run JMeter in headless mode, store output in a result file, monitor progress, stop the test when requested, return a unique test ID, and store results for future comparison.

### Result Collection

The tool should collect response time, average latency, p50, p95, p99, throughput, error rate, HTTP status codes, successful requests, failed requests, duration, and active virtual users.

Percentiles matter because the average hides problems. If p95 is 4 seconds, 5% of users waited at least 4 seconds even if the average looks healthy.

### Automated Analysis

The tool should detect:

- Error rates above configured limits
- p95 latency above an expected limit
- HTTP 5xx responses
- Connection failures
- SQL timeouts
- CPU or memory exhaustion
- Connection-pool exhaustion
- Sudden performance degradation
- Memory growth during soak tests

It should generate readable warnings, for example:

> Error rate reached 5% when 350 users were active. Possible database connection pool exhaustion.

## 8. Script Authoring Workflow (HAR Import to Debug)

### 8.1 Overview

The tool should automate the manual JMeter workflow end to end. The user records traffic and uploads a HAR file. The tool does the rest, and the user reviews and approves at each stage.

| Step | Manual JMeter equivalent | Tool feature |
| --- | --- | --- |
| 1 | Record traffic, export HAR, convert to .jmx | HAR upload, parser and plan generator (8.2) |
| 2 | Delete static and unwanted requests | Request filter (8.3) |
| 3 | Correlate and parameterize | Correlation (8.2) and parameterization (8.4) |
| 4 | Cross-check against DevTools (F12) | HAR comparison validator (8.5) |
| 5 | Add assertions | Assertion builder (8.6) |
| 6 | Run and inspect View Results Tree | Debug run and results viewer (8.7) |
| 7 | Analyze failures, fix, re-run | Failure diagnosis and re-run (8.8) |

Recording itself stays manual in the browser (DevTools Network tab, then export as HAR), because that is where real user behavior is captured. The tool should also let the user export the generated `.jmx` so it can be opened and edited in the JMeter GUI.

### 8.2 HAR Import and Correlation

The tool should accept a browser HAR file and identify dynamic values such as session IDs, authentication tokens, CSRF tokens, cookies, request IDs, user-specific IDs, OAuth tokens, and API keys.

**Why correlation is needed:** the server issues some values per session, such as a token at login. Every later request must send that exact value. Replaying the recorded value usually produces a 401 or 403 response.

The process should:

1. Parse the HAR file.
2. Identify response values reused in later requests.
3. Create JMeter correlation rules.
4. Extract values using regular expressions or JSON extraction (JMeter Regular Expression Extractor or JSON Extractor), storing them in variables such as `${authToken}`.
5. Apply values to subsequent requests.
6. Allow manual correlation when automatic detection is not reliable.

### 8.3 Request Filtering

A browser records everything. One page load can produce more than 100 requests, mostly images, CSS, JavaScript, fonts, and analytics calls. Testing those adds noise, hits servers the team does not own, and distorts results.

After HAR import, the tool should show the full list of captured requests and let the user choose which to keep.

**Automatic exclusion rules (configurable, on by default):**

- Static resources by extension: `.png`, `.jpg`, `.gif`, `.svg`, `.css`, `.js`, `.woff`, `.woff2`, `.ico`, `.map`
- Static resources by MIME type: `image/*`, `text/css`, `font/*`
- Third-party and analytics domains (analytics, ads, tag managers, chat widgets)
- Browser noise: `OPTIONS` preflight requests, `chrome-extension://` calls, cancelled or aborted requests
- Duplicate polling or heartbeat calls (optional)

**Review screen requirements:**

- Table of requests with method, URL, status, size, time, and an include checkbox
- Filters by domain, method, content type, and status code
- Auto-excluded rows shown greyed out and re-includable
- Bulk actions: select all, deselect all, keep API calls only (XHR/fetch, JSON, form posts)
- Ability to reorder requests and group them into named transactions (for example `Login`, `Search`, `Checkout`) so results are reported per business step
- A summary such as "312 captured, 47 kept, 265 excluded"

```yaml
har_filter:
  exclude_extensions: [png, jpg, gif, svg, css, js, woff, woff2, ico, map]
  exclude_mime_types: ["image/*", "text/css", "font/*"]
  exclude_domains:
    - "*.google-analytics.com"
    - "*.doubleclick.net"
    - "*.hotjar.com"
  exclude_methods: [OPTIONS]
  keep_only_xhr_fetch: false
```

### 8.4 Parameterization

If every virtual user sends the same recorded data, the test is unrealistic: servers cache results, reject duplicates, or lock accounts. The tool should replace hard-coded recorded values with variables so each virtual user sends realistic, unique data.

**Requirements:**

1. Detect user-specific input in the HAR: usernames, passwords, emails, search terms, product or order IDs, and form fields.
2. Offer to replace each detected value with a variable such as `${username}`.
3. Support data sources: CSV upload (JMeter CSV Data Set Config), inline lists, and generated values (random numbers, UUIDs, timestamps, sequences).
4. Configure CSV behavior: file path, variable names, delimiter, recycle on end of file, stop thread on end of file, and sharing mode (all threads, per thread group, per thread).
5. Show a preview of which requests, fields, and headers use each variable.
6. Warn when a CSV has fewer rows than virtual users and recycling is off.

```yaml
parameters:
  - name: username
    source: csv
    file: data/users.csv
    column: username
    sharing: all_threads
    recycle: true
  - name: orderId
    source: generated
    type: uuid
  - name: searchTerm
    source: list
    values: [laptop, phone, headphones]
```

**Correlation versus parameterization:**

|  | Correlation | Parameterization |
| --- | --- | --- |
| Value comes from | The server's response | Test data supplied by the team |
| Example | Session token | Username and password |
| Mechanism | Extractor | CSV or generator |

The UI should keep these two lists separate.

### 8.5 HAR Comparison Validator

The tool should verify that the generated script reproduces the original recording. This replaces the manual cross-check against browser DevTools (F12).

**Checks per request:**

| Check | Description |
| --- | --- |
| Endpoint | URL, path, query parameters, and HTTP method match the HAR |
| Headers | Required headers are present (`Authorization`, `Content-Type`, `Cookie`, `X-CSRF-Token`, and others). Browser-managed headers can be ignored |
| Body | Payload structure matches, with dynamic fields replaced by variables |
| Tokens | Every dynamic value is correlated, parameterized, or explicitly marked static |
| Order | Request sequence matches the recorded flow |

**Output requirements:**

- A side-by-side view of the original HAR request and the generated request
- Differences highlighted by type: missing, extra, changed, and unresolved
- A list of unresolved dynamic values, such as a token that appears in a request but has no extractor, each linking to the manual correlation editor
- An overall validation status: Pass, Pass with warnings, or Fail

The validator should run automatically before every debug run and every full test.

### 8.6 Assertion Builder

By default, JMeter treats any response that arrives as a success. A server can return `200 OK` with an error page and be counted as a pass. Assertions define what "correct" means.

**Supported assertion types:**

- Response code (for example `200`, or a range such as `2xx`)
- Response text contains, does not contain, equals, or matches regex
- JSON path (for example `$.status == "ok"`)
- Header value
- Response duration limit (milliseconds)
- Response size range

**Requirements:**

1. Auto-suggest a response code assertion for each request, based on the status in the HAR.
2. Auto-suggest a text or JSON path assertion based on stable fields in the recorded response, such as a success flag or a known title.
3. Allow assertions to be set globally, per transaction, or per request.
4. Count failed assertions separately from HTTP errors in the results.
5. Warn when a request has no assertion at all.

```yaml
assertions:
  - request: POST /api/login
    checks:
      - type: status_code
        expected: 200
      - type: json_path
        path: $.token
        exists: true
      - type: duration
        max_ms: 800
  - request: GET /api/cart
    checks:
      - type: text_contains
        value: "items"
```

### 8.7 Debug Run Mode and Results Viewer

The tool should offer a lightweight debug run to validate the script before applying load. It is the equivalent of JMeter's View Results Tree.

**Debug run behavior:**

- Runs 1 virtual user for 1 iteration (configurable up to a small limit, for example 5 users)
- Stores full request and response details for every sample
- Runs sequentially so the flow matches the recording
- Is clearly labeled as a debug run and is excluded from performance metrics and baselines

**Results viewer requirements (per request):**

- Request: method, URL, headers, body, cookies
- Response: status code, headers, and body (formatted for JSON, XML, and HTML)
- Timing: connect time, latency, total time
- Assertion results: pass or fail with the reason
- Extracted variables: the value captured by each extractor and the value substituted into later requests
- Pass or fail indicator per sample, with a "failed only" filter
- Searchable text within bodies

Full request and response storage should only be enabled for debug runs, never for large load tests, because of storage cost and sensitive data (see section 16).

### 8.8 Failure Diagnosis and Re-run

When a debug or load run has failures, the tool should help the user understand and fix them.

| Symptom | Suggested cause |
| --- | --- |
| 401 or 403 after login | Token or session cookie was not extracted or not applied |
| 403 with a CSRF-related message | CSRF token missing, stale, or not correlated |
| 400 with validation errors | Parameter or body value is wrong, or a dynamic field is still hard-coded |
| 404 on an ID-based path | Recorded ID is not valid for this run, so parameterize it |
| Extractor returned `NOT_FOUND` | Regex or JSON path is wrong, or the previous response changed |
| Assertion fails but status is 200 | Response body differs from the recording (error page or login redirect) |
| Connection refused or timeout | Target unreachable, wrong host, or firewall issue |

**Requirements:**

1. Highlight the first failing request in the flow, since later failures are often caused by it.
2. Show the likely cause and suggested fix next to each failure.
3. Link each suggestion to the screen where it can be fixed (extractor editor, parameter editor, or assertion editor).
4. Provide a one-click **Re-run debug** button that keeps all edits.
5. Keep a history of debug runs for the same script so a failed run can be compared with a later passing run.
6. Provide access to JMeter logs (`jmeter.log`) and the raw `.jtl` for advanced users.

## 9. JMeter Integration

JMeter should handle concurrent virtual users, HTTP and HTTPS requests, REST and SOAP, JDBC, WebSocket and gRPC when configured, distributed load generation, response timing, assertions, and HTML reports.

The tool should handle test configuration, plan generation, HAR conversion, request filtering, correlation, parameterization, validation, lifecycle management, result parsing, dashboard presentation, reporting, CI/CD integration, and error detection.

Example command:

```bash
jmeter -n -t test_plan.jmx -l results.jtl -e -o html_report
```

- `-n` runs without the GUI.
- `-t` is the test plan.
- `-l` is the raw results file (JTL).
- `-e -o` generates the HTML report in the given folder.

The tool should run the command as a background process and expose its status through an API or dashboard.

## 10. Architecture

```text
User Interface
      |
      v
API / Web Application
      |
      v
Test Configuration Service
      |
      v
JMeter Plan Generator  <--  HAR Parser, Request Filter, Correlator,
      |                      Parameterizer, Assertion Builder, Validator
      v
Debug Runner (1 user)  -->  Results Viewer and Diagnosis
      |
      v
JMeter Executor (full load)
      |
      v
Target Application
      |
      v
JTL / Metrics Output
      |
      v
Results Analyzer and Dashboard
```

### Core Modules

1. Scenario and Traffic Profile Builder
2. JMeter Plan Generator
3. HAR Parser and Correlator
4. Execution Engine
5. Metrics Analyzer
6. Dashboard
7. Report Generator
8. CI/CD Integration
9. Request Filter
10. Parameterization Manager
11. HAR Comparison Validator
12. Assertion Builder
13. Debug Runner and Results Viewer
14. Failure Diagnosis Engine

## 11. Recommended Technology Stack

- Backend: Python with FastAPI, Pydantic, Pandas, and SQLAlchemy or SQLite
- Frontend: React or Vue.js, Chart.js or Plotly, Tailwind CSS or Bootstrap
- Execution: Apache JMeter in headless mode
- Containers: Docker
- Data: JSON, CSV, HTML, and SQLite

Go is also suitable because of its strong concurrency support. Python is easier for the initial development and testing workflow.

## 12. Suggested Project Structure and API

```text
performance-tool/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── models/
│   │   │   ├── test_config.py
│   │   │   ├── traffic_profile.py
│   │   │   └── test_result.py
│   │   ├── services/
│   │   │   ├── jmeter_plan_builder.py
│   │   │   ├── har_parser.py
│   │   │   ├── request_filter.py
│   │   │   ├── parameterizer.py
│   │   │   ├── har_validator.py
│   │   │   ├── assertion_builder.py
│   │   │   ├── debug_runner.py
│   │   │   ├── failure_diagnosis.py
│   │   │   ├── result_analyzer.py
│   │   │   └── test_executor.py
│   │   └── routers/
│   │       ├── tests.py
│   │       ├── scripts.py
│   │       ├── debug.py
│   │       └── reports.py
│   ├── tests/
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   └── pages/
│   │       ├── RequestReview.jsx
│   │       ├── Parameters.jsx
│   │       ├── Validation.jsx
│   │       ├── Assertions.jsx
│   │       └── DebugResults.jsx
│   ├── package.json
│   └── vite.config.js
├── config/
│   └── sample.yaml
├── jmeter/
│   ├── templates/
│   └── reports/
└── README.md
```

### Suggested API Endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/scripts/import-har` | Upload a HAR file and return the request list |
| PUT | `/scripts/{id}/filter` | Save include or exclude selections and rules |
| GET | `/scripts/{id}/correlations` | List detected dynamic values |
| PUT | `/scripts/{id}/parameters` | Save parameter and data source configuration |
| POST | `/scripts/{id}/validate` | Run the HAR comparison validator |
| PUT | `/scripts/{id}/assertions` | Save assertions |
| GET | `/scripts/{id}/export-jmx` | Download the generated `.jmx` |
| POST | `/scripts/{id}/debug-run` | Start a 1-user debug run |
| GET | `/debug-runs/{run_id}/samples` | List samples with pass or fail status |
| GET | `/debug-runs/{run_id}/samples/{n}` | Full request, response, and variables for one sample |
| GET | `/debug-runs/{run_id}/diagnosis` | Failure analysis and suggested fixes |
| POST | `/tests` | Create and start a performance test |
| POST | `/tests/{id}/stop` | Stop a running test |
| GET | `/tests/{id}/status` | Live status and metrics |
| GET | `/reports/{id}` | Download the report |

## 13. Implementation Roadmap

### Phase 1: Basic Load Generator

Create a FastAPI application that accepts a target URL, user count, duration, and request method. Generate a basic JMeter plan, run JMeter headlessly, read the result file, display basic metrics, and store results.

### Phase 2: Traffic Profiles

Add constant load, ramp-up, spike, stress, soak, load ramp-down, pauses, and multiple phases.

### Phase 3: HAR Import, Script Preparation, and Debugging

- 3A. Parse the HAR file and generate a basic JMeter plan.
- 3B. Request filtering and review screen (8.3).
- 3C. Dynamic value detection and correlation rules, with manual override (8.2).
- 3D. Parameterization with CSV Data Set support (8.4).
- 3E. HAR comparison validator (8.5).
- 3F. Assertion builder (8.6).
- 3G. Debug run mode and results viewer (8.7).
- 3H. Failure diagnosis and one-click re-run (8.8).

**Exit criteria:** an authenticated multi-step flow recorded from a real browser passes a debug run with all assertions green and no unresolved dynamic values.

### Phase 4: Dashboard

Display test status, latency and throughput charts, errors, alerts, and comparisons between test runs. The debug results viewer from Phase 3G can be reused here.

### Phase 5: Automated Analysis

Calculate p50, p95, and p99 latency, throughput, and error rate. Generate automatic warnings and executive summaries.

### Phase 6: Infrastructure Metrics

Collect CPU and memory usage, database query metrics, and network metrics. Correlate failures with infrastructure events.

### Phase 7: CI/CD Integration

Run tests from GitHub Actions or GitLab CI, compare results with baselines, fail builds when performance thresholds are violated, and publish reports as artifacts.

### Phase 8: Distributed Testing

Add JMeter master-worker architecture, multi-machine execution, cloud workers, and load-generation autoscaling.

### Phase 9: Advanced Features

Add browser tests with Playwright, Core Web Vitals, database data generation, volume testing, scalability testing, breakpoint testing, and Kubernetes or cloud deployment.

## 14. First Working Version

The first version should support one target URL, GET requests, fixed users, fixed duration, JMeter execution, basic metrics, HTML report, API endpoint, and a simple dashboard.

The first version of HAR support should add HAR upload, static-resource filtering, basic correlation, response code assertions, and a single-user debug run with a request and response viewer. Parameterization, the HAR comparison validator, and failure diagnosis follow immediately after.

After that, add traffic profiles, spike and stress testing, real-time charts, automated alerts, CI/CD integration, distributed testing, and volume, soak, scalability, and breakpoint testing.

## 15. Testing Strategy

Use a local sample application before testing production systems. Include:

- A simple REST API
- A fixed-response endpoint
- A deliberately slow endpoint
- An endpoint with controlled HTTP errors
- A login endpoint that returns a session token
- A form with a CSRF token
- An endpoint that requires a unique ID per request
- An endpoint that returns 200 with an error message in the body
- A local database with a known query pattern

The last four-plus items allow end-to-end tests of correlation, parameterization, assertions, and diagnosis. Test real integrations where possible and use mocks only for required integration boundaries.

## 16. Security Requirements

- Do not store credentials in source code. Use environment variables or a secure secret manager.
- Validate target URLs, restrict access to test execution endpoints, and prevent unauthorized internal network access.
- Require authentication for production test execution.
- Log actions and user identities.
- Protect reports and test data.
- HAR files and debug runs contain sensitive data (cookies, tokens, passwords, personal data). Mask these values in the UI and logs by default, with an explicit "reveal" action restricted by role.
- Encrypt stored HAR files and debug samples at rest, and apply a configurable retention period with automatic deletion.
- Offer HAR sanitization on upload (strip or replace credentials before storage).
- Store CSV test data, especially credentials, in protected storage and never in source control.
- Restrict debug runs to approved target environments, the same as full tests.

## 17. Operational Requirements

JMeter should be installed and available in the execution environment. Tests should use isolated target environments. Long-running tests should be cancellable. Results should include test metadata. Failed tests should produce useful logs and reports. Test processes should be cleaned up after completion.

## 18. Definition of Done

A feature is complete when it has documented configuration, a working integration path, stored and downloadable results, clear error handling, automated verification, expected dashboard or report output, and successful local-development testing.

For the script authoring workflow, a feature is also complete when:

- Filtered, correlated, and parameterized scripts can be regenerated deterministically from the same HAR and configuration.
- The HAR comparison validator reports no unresolved dynamic values for the sample application flow.
- A debug run stores and displays full request and response details with sensitive values masked.
- Automated tests cover filtering rules, correlation detection, parameter substitution, and the diagnosis rules, using the local sample application.
- The exported `.jmx` opens and runs in the JMeter GUI without modification.

## 19. Final Recommendation

Start with a small Python or Go application that wraps JMeter. The first release should support load testing, spike testing, stress testing, basic traffic profiles, JMeter execution, metric parsing, simple dashboard, result storage, and HAR correlation with request filtering, assertions, and a debug viewer. Keep the architecture modular so soak, volume, scalability, breakpoint, browser, distributed, and infrastructure-monitoring features can be added later.