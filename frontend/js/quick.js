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
// ---- Traffic profiles ------------------------------------------------------------------
// One step per line, time:users or time:users:ramp. The server turns the steps into thread groups.

const TEST_TYPES = { load: "Load", stress: "Stress", spike: "Spike", soak: "Soak", volume: "Volume",
  scalability: "Scalability", breakpoint: "Breakpoint" };
const UNIT_SECONDS = { ms: 0.001, s: 1, sec: 1, m: 60, min: 60, h: 3600, hr: 3600, d: 86400 };

function parseDuration(text, what) {
  const m = String(text).trim().match(/^(\d+(?:\.\d+)?)\s*(ms|s|sec|m|min|h|hr|d)?$/i);
  if (!m) throw new Error(`${what} "${text}" must be seconds, or a number with s, m or h`);
  return Math.round(+m[1] * UNIT_SECONDS[(m[2] || "s").toLowerCase()]);
}
function parseProfile(text) {
  const lines = text.split("\n").map(line => line.trim()).filter(Boolean);
  return lines.map((line, i) => {
    const parts = line.split(":").map(s => s.trim());
    if (parts.length < 2 || parts.length > 3 || !/^\d+$/.test(parts[1])) {
      throw new Error(`Profile line ${i + 1} must look like time:users or time:users:ramp`);
    }
    return { time_seconds: parseDuration(parts[0], `Line ${i + 1}: the time`), users: +parts[1],
      ramp_seconds: parts[2] ? parseDuration(parts[2], `Line ${i + 1}: the ramp`) : 0 };
  });
}
const showSeconds = (s) => (s >= 3600 && s % 3600 === 0 ? `${s / 3600}h` : s >= 60 && s % 60 === 0 ? `${s / 60}m` : `${s}s`);
const profileText = (steps) => steps.map(s => `${showSeconds(s.time_seconds)}:${s.users}${s.ramp_seconds ? `:${showSeconds(s.ramp_seconds)}` : ""}`).join("\n");

// The planned number of users as the corners of a line (the server's load_profile.curve).
function profileCurve(profile, duration, users = 0, ramp = 0) {
  if (!profile || !profile.length) return [{ t: 0, users: 0 }, { t: ramp, users }, { t: duration, users }];
  const points = [{ t: 0, users: 0 }];
  let level = 0;
  for (const s of profile) {
    if (s.users === level) continue;
    points.push({ t: s.time_seconds, users: level }, { t: s.time_seconds + s.ramp_seconds, users: s.users });
    level = s.users;
  }
  points.push({ t: duration, users: level });
  return points;
}
function usersAt(curve, t) {
  let value = 0;
  for (let i = 0; i < curve.length; i++) {
    const p = curve[i];
    if (p.t <= t) { value = p.users; continue; }
    const q = curve[i - 1];
    if (q && p.t > q.t) value = q.users + (p.users - q.users) * (t - q.t) / (p.t - q.t);
    break;
  }
  return value;
}

// A starting point for each test type, with the peak and duration typed in the form.
function profileTemplate(type, users, duration) {
  const u = Math.max(1, users), d = Math.max(10, duration);
  const at = (f) => Math.round(d * f), share = (f) => Math.max(1, Math.round(u * f));
  const rows = {
    stress: [[0, share(0.25)], [at(0.2), share(0.5)], [at(0.4), share(0.75)], [at(0.6), u]],
    spike: [[0, share(0.1)], [at(0.3), u], [at(0.5), share(0.1)]],
    scalability: [[0, share(1 / 3)], [at(1 / 3), share(2 / 3)], [at(2 / 3), u]],
    breakpoint: [[0, 1], [1, u, d - 2]],
  }[type];
  if (!rows) return { profile: [], ramp: type === "soak" ? Math.min(300, at(0.1)) : at(0.1) };
  const steps = [];
  for (const [t, n, r = 0] of rows) if (!steps.length || steps[steps.length - 1].users !== n) steps.push({ time_seconds: t, users: n, ramp_seconds: r });
  return { profile: steps.length > 1 ? steps : [], ramp: steps.length > 1 ? 0 : at(0.1) };
}

// A new test type fills in its template; changing the peak or duration keeps a generated profile
// in step. Anything the user wrote is left alone.
let lastTemplate = "";
function applyTemplate(form, typeChanged) {
  const el = form.elements;
  const current = el.profile.value.trim();
  if (current !== lastTemplate && (current || !typeChanged)) return;
  const t = profileTemplate(el.test_type.value, +el.users.value, +el.duration_seconds.value);
  lastTemplate = profileText(t.profile);
  el.profile.value = lastTemplate;
  if (typeChanged) el.ramp_up_seconds.value = t.ramp;
  if (typeChanged && el.test_type.value === "breakpoint" && !el.stop_error_rate.value && !el.stop_p95_ms.value) {
    el.stop_error_rate.value = 5;
  }
}

function profileSvg(curve, peak, duration) {
  const ns = "http://www.w3.org/2000/svg", W = 300, H = 64;
  const x = (t) => (duration ? (t / duration) * W : 0), y = (u) => H - 2 - (peak ? (u / peak) * (H - 6) : 0);
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  const line = curve.map(p => `${x(p.t).toFixed(1)},${y(p.users).toFixed(1)}`).join(" ");
  const area = document.createElementNS(ns, "polygon");
  area.setAttribute("points", `0,${H - 2} ${line} ${W},${H - 2}`);
  area.setAttribute("fill", "rgba(15,110,140,.14)");
  const stroke = document.createElementNS(ns, "polyline");
  stroke.setAttribute("points", line);
  stroke.setAttribute("fill", "none");
  stroke.setAttribute("stroke", "#0f6e8c");
  stroke.setAttribute("stroke-width", "2");
  stroke.setAttribute("vector-effect", "non-scaling-stroke");
  svg.append(area, stroke);
  return svg;
}

function renderProfilePreview(form) {
  const box = $("#profilepreview");
  if (!box || !document.createElementNS) return;
  const el = form.elements;
  box.className = "profilepreview";
  try {
    const profile = parseProfile(el.profile.value), duration = +el.duration_seconds.value || 0;
    const curve = profileCurve(profile, duration, +el.users.value, +el.ramp_up_seconds.value);
    const peak = Math.max(...curve.map(p => p.users));
    const svg = profileSvg(curve, peak, duration);
    svg.setAttribute("aria-label", `Planned users: up to ${peak} over ${duration} seconds`);
    box.replaceChildren(svg, h("div", { class: "axis" }, h("span", {}, "0 s"), h("span", {}, `peak ${fmt(peak)} users`),
      h("span", {}, `${fmt(duration)} s`)));
  } catch (e) { box.className = "profilepreview bad"; box.replaceChildren(e.message); }
}

$("#form").addEventListener("change", (e) => {
  if (e.target.name === "test_type") applyTemplate(e.currentTarget, true);
  renderProfilePreview(e.currentTarget);
});
$("#form").addEventListener("input", (e) => {
  if (["users", "duration_seconds"].includes(e.target.name)) applyTemplate(e.currentTarget, false);
  if (["profile", "users", "duration_seconds", "ramp_up_seconds"].includes(e.target.name)) renderProfilePreview(e.currentTarget);
});
renderProfilePreview($("#form"));

function formToConfig(f) {
  const v = (n) => f.elements[n].value;
  const cfg = {
    name: v("name").trim(), target_url: v("target_url").trim(), method: v("method"), test_type: v("test_type"),
    users: +v("users"), duration_seconds: +v("duration_seconds"), ramp_up_seconds: +v("ramp_up_seconds"),
    think_time_ms: +v("think_time_ms"), timeout_ms: +v("timeout_ms"),
    expected_status_codes: v("expected_status_codes").split(",").map(s => parseInt(s, 10)).filter(Number.isInteger),
    headers: parseHeaders(v("headers")),
    thresholds: { max_error_rate_percent: +v("max_error_rate_percent"), max_p95_ms: v("max_p95_ms") ? +v("max_p95_ms") : null },
  };
  const profile = parseProfile(v("profile"));
  if (profile.length) {               // the profile's highest step is the peak; steps carry their own ramps
    cfg.profile = profile; delete cfg.users; cfg.ramp_up_seconds = 0;
  }
  if (v("stop_error_rate") || v("stop_p95_ms")) {
    cfg.stop_when = { error_rate_percent: v("stop_error_rate") ? +v("stop_error_rate") : null,
      p95_ms: v("stop_p95_ms") ? +v("stop_p95_ms") : null, for_seconds: +v("stop_for_seconds") || 10 };
  }
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
  const bp = m.breaking_point;
  if (bp) return { cls: "fail", text: `Breaking point: ${fmt(bp.users)} users, when ${bp.reason}. Stopped after ${fmt(bp.t)} s. ${facts}` };
  if (m.verdict === "pass") return { cls: "pass", text: `Passed. ${facts}` };
  return { cls: "fail", text: `Failed. ${facts}` };
}

// "Spike · 5 → 500 → 5 users over 60s" or "Load · 10 users · 60s with 6s ramp-up".
function loadDescription(c) {
  const type = TEST_TYPES[c.test_type || "load"] || "Load";
  if (c.profile) {
    const steps = c.profile.map(s => `${s.users}${s.ramp_seconds ? ` (over ${showSeconds(s.ramp_seconds)})` : ""} at ${showSeconds(s.time_seconds)}`);
    return `${type} · ${steps.join(" → ")} · ${c.duration_seconds}s`;
  }
  return `${type} · ${c.users} users · ${c.duration_seconds}s${c.ramp_up_seconds ? ` with ${c.ramp_up_seconds}s ramp-up` : ""}`;
}

function failureBreakdown(m) {
  if (!m.failed_requests) return null;
  const parts = [[m.status_code_failures, "wrong status code"], [m.assertion_failures, "failed a check"],
    [m.connection_failures, "could not connect or timed out"]].filter(([n]) => n);
  return parts.length ? h("p", { class: "empty" }, `Failed requests: ${parts.map(([n, t]) => `${fmt(n)} ${t}`).join(" · ")}.`) : null;
}

function drawCharts(points, config, bucket) {
  const labels = points.map(p => `${p.t}s`);
  const plan = config ? profileCurve(config.profile, config.duration_seconds, config.users, config.ramp_up_seconds) : null;
  // Like the active users (the most seen in each interval), the most users planned in each interval.
  const planned = (t) => Math.round(Math.max(usersAt(plan, t), usersAt(plan, t + bucket - 0.001)));
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
      ...(plan ? [{ label: "Planned users", data: points.map(p => planned(p.t)), yAxisID: "users", stepped: true,
        borderColor: "#5a6b7b", backgroundColor: "#5a6b7b", borderWidth: 1.5, borderDash: [3, 3], pointRadius: 0, order: 4 }] : []),
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
        h("div", { class: "url" }, `${c.method} ${c.target_url} · ${loadDescription(c)}${c.stop_when ? " · stops at the breaking point" : ""}${headersShown ? ` · ${headersShown} custom header${headersShown > 1 ? "s" : ""}` : ""}`),
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
    m && m.total_requests ? failureBreakdown(m) : null,

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

  if (points.length) drawCharts(points, c, points.length > 1 ? points[1].t - points[0].t : 2);
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
