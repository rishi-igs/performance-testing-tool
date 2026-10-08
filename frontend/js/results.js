"use strict";
// A scenario run: live while it runs, then LoadRunner-style results per business function.

const PALETTE = ["#0f6e8c", "#b3261e", "#1f7a4d", "#a66a00", "#5b3b8c", "#16222e", "#c2185b", "#00796b"];

function scenarioOutcome(run) {
  if (run.status === "running") return { cls: "running", label: "running" };
  if (run.status === "completed" && run.summary) {
    if (run.summary.verdict === "pass") return { cls: "completed", label: "passed" };
    if (run.summary.verdict === "fail") return { cls: "failed", label: "failed SLA" };
  }
  return { cls: run.status === "stopped" ? "stopped" : "failed", label: run.status };
}

async function openScenarioRun(scenarioId, runId) {
  clearTimeout(state.timer);
  state.scenarioId = scenarioId; state.runId = runId;
  let run;
  try { run = await api(`/tests/${runId}`); } catch (e) { setMain(h("div", { class: "notice" }, e.message)); return; }
  const total = run.config.duration_seconds || 60;
  const bucket = total <= 120 ? 2 : total <= 900 ? 5 : 30;
  const timeline = await api(`/tests/${runId}/timeline?bucket_seconds=${bucket}`).catch(() => null);
  if (run.status !== "running") {
    state.scenario = await api(`/scenarios/${scenarioId}`).catch(() => state.scenario);   // its latest runs, to compare with
    await loadAnalysis(runId, bucket);
  }
  if (state.section !== "scenarios" || state.runId !== runId) return;     // the user moved on
  renderScenarioRun(run, timeline);
  if (run.status === "running") state.timer = setTimeout(() => openScenarioRun(scenarioId, runId), 2000);
  else refreshScenarios();
}

async function stopScenarioRun() {
  try { await api(`/tests/${state.runId}/stop`, { method: "POST" }); openScenarioRun(state.scenarioId, state.runId); }
  catch (e) { alert(e.message); }
}

function lineChart(id, labels, datasets, yTitle, extra = {}) {
  const el = document.getElementById(id);
  if (!el || typeof Chart === "undefined") return;
  state.charts[id] = new Chart(el, { type: "line", data: { labels, datasets },
    options: { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } } },
      scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 12 } },
        y: { beginAtZero: true, title: { display: true, text: yTitle } }, ...extra } } });
}

function seriesLabels(series) {
  const ts = [...new Set(Object.values(series).flatMap(points => points.map(p => p.t)))].sort((a, b) => a - b);
  return ts;
}
function aligned(points, ts, key) {
  const at = new Map(points.map(p => [p.t, p[key]]));
  return ts.map(t => (at.has(t) ? at.get(t) : null));
}

function drawRunCharts(tl) {
  if (!tl || !tl.groups) return;
  const ts = seriesLabels({ ...tl.groups, ...tl.transactions });
  const labels = ts.map(t => `${t}s`);
  const ds = (series, key, opts = {}) => Object.entries(series).map(([name, points], i) => ({
    label: name, data: aligned(points, ts, key), borderColor: PALETTE[i % PALETTE.length], backgroundColor: PALETTE[i % PALETTE.length],
    pointRadius: 0, borderWidth: 2, spanGaps: true, tension: .2, ...opts }));
  lineChart("chart-users", labels, ds(tl.groups, "users", { stepped: true, tension: 0 }), "Running users");
  lineChart("chart-tx", labels, ds(tl.transactions, "p90_ms"), "90th percentile (ms)");
  lineChart("chart-tps", labels, ds(tl.transactions, "tps"), "Per second");
}

function transactionTable(m, sla) {
  const status = new Map();
  for (const r of sla || []) {
    const prev = status.get(r.transaction);
    if (r.status === "fail" || !prev) status.set(r.transaction, r.status);
  }
  const rows = m.transactions;
  if (!rows.length) return h("p", { class: "empty" }, "No business function has finished yet.");
  return h("div", { class: "tablewrap" }, h("table", { class: "grid results" },
    h("thead", {}, h("tr", {}, ["Business function", "SLA", "Count", "Passed", "Failed", "Min", "Average", "Max", "90%", "95%", "Per second"]
      .map(t => h("th", { scope: "col" }, t)))),
    h("tbody", {}, rows.map(t => h("tr", { class: t.failed ? "failrow" : null },
      h("td", {}, t.display),
      h("td", {}, status.has(t.display) ? h("span", { class: `badge ${status.get(t.display) === "pass" ? "completed" : status.get(t.display) === "fail" ? "failed" : "stopped"}` },
        status.get(t.display) === "pass" ? "met" : status.get(t.display) === "fail" ? "missed" : "no data") : "–"),
      h("td", { class: "num" }, fmt(t.count)), h("td", { class: "num" }, fmt(t.passed)), h("td", { class: "num" }, fmt(t.failed)),
      h("td", { class: "num" }, `${fmt(t.min)} ms`), h("td", { class: "num" }, `${fmt(t.avg, 1)} ms`), h("td", { class: "num" }, `${fmt(t.max)} ms`),
      h("td", { class: "num" }, `${fmt(t.p90)} ms`), h("td", { class: "num" }, `${fmt(t.p95)} ms`), h("td", { class: "num" }, fmt(t.tps, 2)))))));
}

function slaTable(sla) {
  if (!sla || !sla.length) return null;
  const unit = (m) => (m === "error_rate" ? "%" : m === "tps_min" ? "/s" : " ms");
  const label = Object.fromEntries(SLA_METRICS);
  return h("div", { class: "section" }, h("h2", {}, "Service level agreements"),
    h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["Business function", "Measure", "Limit", "Actual", "Result"].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, sla.map(r => h("tr", { class: r.status === "fail" ? "failrow" : null },
        h("td", {}, r.transaction), h("td", {}, label[r.metric]),
        h("td", { class: "num" }, `${fmt(r.limit, 2)}${unit(r.metric)}`),
        h("td", { class: "num" }, r.actual == null ? "–" : `${fmt(r.actual, 2)}${unit(r.metric)}`),
        h("td", {}, h("span", { class: `badge ${r.status === "pass" ? "completed" : r.status === "fail" ? "failed" : "stopped"}` },
          r.status === "pass" ? "met" : r.status === "fail" ? "missed" : "no data"))))))));
}

function renderScenarioRun(run, tl, focusKey) {
  const scrollY = window.scrollY;
  const m = run.summary, c = run.config, id = run.id, running = run.status === "running";
  const o = scenarioOutcome(run);
  clearCharts();
  let pct = 0;
  if (running && run.started_at) pct = Math.min(100, ((Date.now() - Date.parse(run.started_at)) / 1000) / (c.duration_seconds || 1) * 100);
  const failedSla = m && m.sla ? m.sla.filter(r => r.status === "fail").length : 0;
  const verdict = running ? "Running. Results update every two seconds."
    : run.status === "failed" && run.error ? run.error
      : !m || m.verdict === "no_data" ? "The run produced no results."
        : `${m.verdict === "pass" ? "Passed" : "Failed"}: ${fmt(m.requests.total_requests)} requests, ${fmt(m.requests.error_rate_percent, 2)}% errors`
          + (m.sla.length ? `, ${failedSla ? `${failedSla} of ${m.sla.length} SLA checks missed` : `all ${m.sla.length} SLA checks met`}.` : ".");
  const dl = (path, label) => h("a", { class: "btn quiet", href: `/reports/${id}/${path}`, target: path === "" ? "_blank" : null, rel: "noopener" }, label);

  setMain(
    h("p", {}, h("a", { href: `#/scenarios/${state.scenarioId}` }, "← Scenario")),
    h("div", { class: "head" },
      h("div", {},
        h("h1", {}, run.name, " ", h("span", { class: `badge ${o.cls}` }, o.label)),
        h("div", { class: "url" }, `${fmt(c.total_users)} users · ${fmt(c.duration_seconds)} s planned · `
          + c.groups.map(g => `${g.name}: ${g.users} × ${g.script}`).join(" · ")),
        h("p", { class: `verdict ${running ? "" : m && m.verdict === "pass" ? "pass" : "fail"}` }, verdict)),
      running ? h("button", { class: "btn danger", onclick: stopScenarioRun }, "Stop") : null),
    running ? h("div", { class: "progress" }, h("div", { style: `width:${pct}%` })) : null,
    (c.warnings || []).map(w => h("div", { class: "warn" }, w)),

    m && m.requests && m.requests.total_requests ? h("div", { class: "stats" },
      stat(fmt(m.requests.total_requests), "Requests"),
      stat(`${fmt(m.requests.error_rate_percent, 2)}%`, `Errors (${fmt(m.requests.failed_requests)})`),
      stat(fmt(m.requests.throughput_rps, 1), "Requests per second"),
      stat(`${fmt(m.requests.response_time_ms.p95)} ms`, "Request p95"),
      stat(fmt(m.requests.peak_users), "Peak users")) : null,
    m && m.goal ? h("div", { class: m.goal.reached ? "notice good" : "warn" },
      `Goal: ${fmt(m.goal.target, 2)} ${m.goal.type === "hits_per_second" ? "requests" : "business functions"} per second, `
      + `reached ${fmt(m.goal.actual, 2)}/s once every user was running${m.goal.reached ? "." : " (not reached)."}`) : null,

    h("div", { class: "hero" }, h("h2", {}, "Running users per group"), h("div", { class: "chartbox small" }, h("canvas", { id: "chart-users" }))),
    h("div", { class: "hero" }, h("h2", {}, "Response time per business function (90th percentile)"), h("div", { class: "chartbox small" }, h("canvas", { id: "chart-tx" }))),
    h("div", { class: "hero" }, h("h2", {}, "Business functions per second"), h("div", { class: "chartbox small" }, h("canvas", { id: "chart-tps" }))),

    h("div", { class: "section" }, h("h2", {}, "Transaction summary"), m ? transactionTable(m, m.sla) : h("p", { class: "empty" }, "Waiting for results…")),
    m ? slaTable(m.sla) : null,
    m && m.groups && m.groups.length ? h("div", { class: "section" }, h("h2", {}, "Groups"),
      h("div", { class: "tablewrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["Group", "Peak users", "Requests", "Errors"].map(t => h("th", { scope: "col" }, t)))),
        h("tbody", {}, m.groups.map(g => h("tr", {}, h("td", {}, g.name), h("td", { class: "num" }, fmt(g.peak_users)),
          h("td", { class: "num" }, fmt(g.requests)), h("td", { class: "num" }, `${fmt(g.error_rate_percent, 2)}%`))))))) : null,
    m && m.warnings && m.warnings.length ? h("div", { class: "section" }, h("h2", {}, "Findings"),
      m.warnings.map(w => h("div", { class: `warn ${w.severity}` }, w.message))) : null,
    running ? null : analysisSection(run, (key) => renderScenarioRun(run, tl, key)),
    h("div", { class: "section" }, h("h2", {}, "Downloads"),
      h("div", { class: "links" }, running ? null : dl("", "JMeter HTML report"), dl("metrics.json", "Metrics (JSON)"),
        dl("results.csv", "Raw results (CSV)"), dl("plan.jmx", "Test plan (.jmx)"), dl("jmeter.log", "JMeter log"))));
  drawRunCharts(tl);
  if (!running) drawAnalysis();
  window.scrollTo(0, scrollY);
  restoreFocus(focusKey);
}
