"use strict";
// Quick test: one URL, a number of users, a duration. Results update live while it runs.

function parseHeaders(text) {
  const headers = {};
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const i = line.indexOf(":");
    if (i < 1) throw new Error(`Header "${line}" must look like Name: value`);
    headers[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return headers;
}
function parseProfile(text) {
  const lines = text.split("\n").map(line => line.trim()).filter(Boolean);
  return lines.map((line, i) => {
    const match = line.match(/^(\d+)\s*:\s*(\d+)$/);
    if (!match) throw new Error(`Profile line ${i + 1} must look like seconds:users`);
    return { time_seconds: +match[1], users: +match[2] };
  });
}
function formToConfig(f) {
  const v = (n) => f.elements[n].value;
  const cfg = {
    name: v("name").trim(), target_url: v("target_url").trim(), method: v("method"),
    users: +v("users"), duration_seconds: +v("duration_seconds"), ramp_up_seconds: +v("ramp_up_seconds"),
    think_time_ms: +v("think_time_ms"), timeout_ms: +v("timeout_ms"),
    expected_status_codes: v("expected_status_codes").split(",").map(s => parseInt(s, 10)).filter(Number.isInteger),
    headers: parseHeaders(v("headers")),
    thresholds: { max_error_rate_percent: +v("max_error_rate_percent"), max_p95_ms: v("max_p95_ms") ? +v("max_p95_ms") : null },
  };
  const profile = parseProfile(v("profile"));
  if (profile.length) cfg.profile = profile;
  if (v("body").trim() && v("method") !== "GET") cfg.body = v("body");
  return cfg;
}
$("#form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#start"), err = $("#formerror");
  err.textContent = ""; btn.disabled = true;
  try {
    const created = await api(inProject("/tests"), { method: "POST", body: JSON.stringify(formToConfig(e.target)) });
    state.selected = created.id;
    await refreshRuns();
    go(`#/quick/${created.id}`);
  } catch (ex) { err.textContent = ex.message; }
  finally { btn.disabled = false; }
});

// ---- Run list --------------------------------------------------------------------------
// "completed" only means JMeter finished. Show whether the results met the limits.
function outcome(run) {
  if (run.status === "completed" && run.summary) {
    if (run.summary.verdict === "pass") return { cls: "completed", label: "passed" };
    if (run.summary.verdict === "fail") return { cls: "failed", label: "failed limits" };
  }
  return { cls: run.status, label: run.status };
}
const badge = (run) => { const o = outcome(run); return h("span", { class: `badge ${o.cls}` }, o.label); };

async function refreshRuns() {
  state.runs = await api(inProject("/tests"));
  const box = $("#runs");
  if (!state.runs.length) { box.replaceChildren(h("p", { class: "empty" }, "No runs yet. Start your first test above.")); return; }
  box.replaceChildren(...state.runs.map(r => h("a", {
      class: "run", href: `#/quick/${r.id}`, "aria-current": r.id === state.selected && state.section === "quick" ? "page" : null },
    h("div", { class: "name" }, r.name),
    h("div", { class: "sub" }, badge(r), " ", ago(r.created_at)))));
}

// ---- Run detail ------------------------------------------------------------------------
function verdictLine(run) {
  const m = run.summary;
  if (run.status === "running") return null;
  if (run.status === "failed" && run.error) return { cls: "fail", text: run.error };
  if (!m || m.verdict === "no_data") return { cls: "fail", text: "The test produced no results." };
  const facts = `${fmt(m.total_requests)} requests, ${fmt(m.error_rate_percent, 2)}% errors, p95 ${fmt(m.response_time_ms.p95)} ms.`;
  if (m.verdict === "pass") return { cls: "pass", text: `Passed. ${facts}` };
  return { cls: "fail", text: `Failed. ${facts}` };
}

function drawCharts(points) {
  const labels = points.map(p => `${p.t}s`);
  const base = { responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false }, plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } } },
    scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 12 } } } };

  const mk = (id, cfg) => {
    if (state.charts[id]) { state.charts[id].data = cfg.data; state.charts[id].update("none"); return; }
    const el = document.getElementById(id); if (!el) return;
    state.charts[id] = new Chart(el, cfg);
  };
  mk("chart-latency", { type: "line", data: { labels, datasets: [
      { label: "Active users", data: points.map(p => p.users), yAxisID: "users", fill: true, stepped: true,
        borderWidth: 0, backgroundColor: "rgba(15,110,140,.14)", pointRadius: 0, order: 3 },
      { label: "p95 response time (ms)", data: points.map(p => p.p95_ms), yAxisID: "ms", borderColor: "#0f6e8c", backgroundColor: "#0f6e8c", borderWidth: 2.5, pointRadius: 0, tension: .25, order: 1 },
      { label: "Average (ms)", data: points.map(p => p.avg_ms), yAxisID: "ms", borderColor: "#16222e", backgroundColor: "#16222e", borderWidth: 1.5, borderDash: [5, 4], pointRadius: 0, tension: .25, order: 2 } ] },
    options: { ...base, scales: { ...base.scales,
      ms: { position: "left", beginAtZero: true, title: { display: true, text: "Response time (ms)" } },
      users: { position: "right", beginAtZero: true, grid: { display: false }, title: { display: true, text: "Active users" } } } } });
  mk("chart-throughput", { type: "bar", data: { labels, datasets: [
      { type: "line", label: "Requests per second", data: points.map(p => p.rps), yAxisID: "rps", borderColor: "#0f6e8c", backgroundColor: "#0f6e8c", borderWidth: 2, pointRadius: 0, tension: .25 },
      { type: "bar", label: "Error rate (%)", data: points.map(p => p.error_rate_percent), yAxisID: "err", backgroundColor: "rgba(179,38,30,.55)" } ] },
    options: { ...base, scales: { ...base.scales,
      rps: { position: "left", beginAtZero: true, title: { display: true, text: "Requests/s" } },
      err: { position: "right", beginAtZero: true, max: 100, grid: { display: false }, title: { display: true, text: "Errors (%)" } } } } });
}

async function showRun() {
  clearTimeout(state.timer);
  if (state.section !== "quick") return;
  if (!state.selected) { renderWelcome(); return; }
  let run;
  try { run = await api(`/tests/${state.selected}`); } catch (e) { setMain(h("div", { class: "notice" }, e.message)); return; }
  const bucket = run.config.duration_seconds <= 120 ? 2 : run.config.duration_seconds <= 900 ? 5 : 30;
  const timeline = await api(`/tests/${state.selected}/timeline?bucket_seconds=${bucket}`).catch(() => ({ points: [] }));
  if (run.status !== "running") await loadAnalysis(run.id, bucket);
  if (state.section !== "quick" || state.selected !== run.id) return;   // the user moved on while this loaded
  renderRun(run, timeline.points);
  if (run.status === "running") state.timer = setTimeout(() => { refreshRuns(); showRun(); }, 2000);
  else refreshRuns();
}

function renderRun(run, points, focusKey) {
  const scrollY = window.scrollY;   // live runs re-render every 2s: keep the reader's place
  const m = run.summary, c = run.config, v = verdictLine(run), id = run.id;
  const running = run.status === "running";
  let pct = 0;
  if (running && run.started_at) pct = Math.min(100, ((Date.now() - Date.parse(run.started_at)) / 1000) / c.duration_seconds * 100);

  const dl = (path, label) => h("a", { class: "btn quiet", href: `/reports/${id}/${path}`, target: path === "" ? "_blank" : null, rel: "noopener" }, label);
  const headersShown = Object.keys(c.headers || {}).length;
  clearCharts();

  setMain(
    h("div", { class: "head" },
      h("div", {},
        h("h1", {}, run.name, " ", badge(run)),
        h("div", { class: "url" }, `${c.method} ${c.target_url} · ${c.profile ? `step load to ${c.users} users` : `${c.users} users`} · ${c.duration_seconds}s${c.profile ? ` · ${c.profile.map(s => `${s.users} at ${s.time_seconds}s`).join(" → ")}` : c.ramp_up_seconds ? ` with ${c.ramp_up_seconds}s ramp-up` : ""}${headersShown ? ` · ${headersShown} custom header${headersShown > 1 ? "s" : ""}` : ""}`),
        running ? h("p", { class: "verdict" }, "Running. Results update every two seconds.") : (v && h("p", { class: `verdict ${v.cls}` }, v.text))),
      running ? h("button", { class: "btn danger", onclick: stopRun }, "Stop test") : null),
    running ? h("div", { class: "progress" }, h("div", { style: `width:${pct}%` })) : null,

    h("div", { class: "hero" }, h("h2", {}, "Response time and load"),
      h("div", { class: "chartbox" }, h("canvas", { id: "chart-latency" }), points.length ? null : h("div", { class: "nodata" }, running ? "Waiting for the first results…" : "No data recorded"))),

    m && m.total_requests ? h("div", { class: "stats" },
      stat(fmt(m.total_requests), "Requests"),
      stat(`${fmt(m.error_rate_percent, 2)}%`, `Errors (${fmt(m.failed_requests)})`),
      stat(`${fmt(m.response_time_ms.p50)} ms`, "Median"),
      stat(`${fmt(m.response_time_ms.p95)} ms`, "p95"),
      stat(`${fmt(m.response_time_ms.p99)} ms`, "p99"),
      stat(fmt(m.throughput_rps, 1), "Requests per second"),
      stat(fmt(m.peak_users), "Peak users")) : null,

    m && m.total_requests ? h("div", { class: "hero" }, h("h2", {}, "Throughput and errors"),
      h("div", { class: "chartbox small" }, h("canvas", { id: "chart-throughput" }))) : null,

    m && m.total_requests ? h("div", { class: "section" }, h("h2", {}, "Findings"),
      m.warnings.length ? m.warnings.map(w => h("div", { class: `warn ${w.severity}` }, w.message))
        : h("p", { class: "allclear" }, running ? "No problems detected so far." : "No problems found against your limits.")) : null,

    m && m.total_requests ? h("div", { class: "section" }, h("h2", {}, "Status codes"),
      h("div", { class: "codes" }, Object.entries(m.status_codes).map(([k, n]) => h("span", { class: "code" }, `${k}: ${fmt(n)}`)))) : null,

    running || !m || !m.total_requests ? null : analysisSection(run, (key) => renderRun(run, points, key)),
    h("div", { class: "section" }, h("h2", {}, "Downloads"),
      h("div", { class: "links" },
        running ? null : dl("", "JMeter HTML report"), dl("metrics.json", "Metrics (JSON)"), dl("results.csv", "Raw results (CSV)"),
        dl("plan.jmx", "Test plan (.jmx)"), dl("jmeter.log", "JMeter log"))));

  if (points.length) drawCharts(points);
  if (!running) drawAnalysis();
  window.scrollTo(0, scrollY);
  restoreFocus(focusKey);
}

async function stopRun() {
  try { await api(`/tests/${state.selected}/stop`, { method: "POST" }); await showRun(); }
  catch (e) { alert(e.message); }
}

function renderWelcome() {
  $("#main").replaceChildren(h("div", { class: "welcome" },
    h("h1", {}, "Find slow spots before your users do"),
    h("p", {}, "Enter a URL, pick how many virtual users should call it and for how long, then start the test. Results appear here while it runs."),
    h("p", {}, "To test a whole user flow, open Scripts and import a HAR recording or a JMeter .jmx."),
    h("p", {}, "Only test systems you own or have permission to test.")));
}
