"use strict";
// On a saved scenario: scheduled runs (nightly and the like) and trends across its runs.

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const TREND_COLORS = ["#0f6e8c", "#b3261e", "#1f7a4d", "#a66a00", "#5b3b8c", "#16222e"];

function browserZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (_) { return "UTC"; }
}
const localTime = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

async function loadSchedulesAndTrends(id) {
  const [schedules, trends] = await Promise.all([
    api(`/schedules?scenario_id=${encodeURIComponent(id)}`).catch(() => []),
    api(`/scenarios/${id}/trends`).catch(() => null)]);
  state.schedules = schedules;
  state.trends = trends;
}

function describeSchedule(c) {
  const zone = c.timezone && c.timezone !== browserZone() ? ` (${c.timezone})` : "";
  if (c.repeat === "once") return `Once, on ${c.at.replace("T", " at ")}${zone}`;
  if (c.repeat === "daily") return `Every day at ${c.time}${zone}`;
  return `Every ${c.days.map(d => DAY_NAMES[d]).join(", ")} at ${c.time}${zone}`;
}

async function scheduleCall(path, opts) {
  scenarioError("");
  try { await api(path, opts); } catch (e) { scenarioError(`Schedule not saved: ${e.message}`); return; }
  await loadSchedulesAndTrends(state.scenarioId);
  renderScenario(focusedKey());
}

function addSchedule(e) {
  e.preventDefault();
  const el = e.target.elements, repeat = state.scheduleDraft.repeat;
  const body = { scenario_id: state.scenarioId, repeat, timezone: browserZone() };
  if (repeat === "once") {
    body.at = el.at.value;
    body.utc_offset_minutes = -new Date(el.at.value).getTimezoneOffset();   // the offset on that day
  } else {
    body.time = el.time.value;
    body.utc_offset_minutes = -new Date().getTimezoneOffset();
  }
  if (repeat === "weekly") body.days = DAY_NAMES.map((_, i) => i).filter(i => el[`day${i}`].checked);
  return scheduleCall("/schedules", { method: "POST", body: JSON.stringify(body) });
}

function scheduleRow(s, x) {
  const label = describeSchedule(x.config);
  return h("tr", {},
    h("td", {}, h("input", { type: "checkbox", checked: x.config.enabled, disabled: !can("tester"), "aria-label": `${label}: on`,
      onchange: (e) => scheduleCall(`/schedules/${x.id}`, { method: "PUT", body: JSON.stringify({ ...x.config, enabled: e.target.checked }) }) })),
    h("td", {}, label, x.zone_mode === "offset"
      ? h("small", { class: "src" }, "Fixed UTC offset: daylight saving is not followed until the server has tzdata") : null),
    h("td", {}, x.next_run_at ? localTime(x.next_run_at) : h("span", { class: "empty" }, x.config.enabled ? "none left" : "off")),
    h("td", {}, x.last_run_id ? h("a", { href: `#/scenarios/${s.id}/run/${x.last_run_id}` }, localTime(x.last_run_at)) : null,
      x.last_error ? h("small", { class: "src" }, x.last_error) : x.last_run_id ? null : h("span", { class: "empty" }, "not yet")),
    h("td", { class: "rowactions" }, can("tester") ? h("button", { class: "btn quiet small", type: "button", "aria-label": `Remove: ${label}`,
      onclick: () => scheduleCall(`/schedules/${x.id}`, { method: "DELETE" }) }, "Remove") : null));
}

function schedulesSection(s) {
  const draft = state.scheduleDraft, list = state.schedules || [];
  return h("div", { class: "section" }, h("h2", {}, "Scheduled runs"),
    h("p", { class: "intro" }, "Run this scenario automatically, for example every night. Scheduled runs pass the same limits and target checks as a run you start. A run that falls due while the server is down is skipped, not started late."),
    list.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["On", "When", "Next run", "Last run", ""].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, list.map(x => scheduleRow(s, x))))) : h("p", { class: "empty" }, "Not scheduled."),
    can("tester") ? h("form", { class: "inline", "aria-label": "Add a scheduled run", onsubmit: addSchedule },
      field("Repeat", choice("repeat", [["once", "Once"], ["daily", "Every day"], ["weekly", "Every week"]], draft.repeat,
        { "data-key": "sch-repeat", onchange: (e) => { draft.repeat = e.target.value; renderScenario("sch-repeat"); } })),
      draft.repeat === "once"
        ? field("Date and time", h("input", { name: "at", type: "datetime-local", required: true, "data-key": "sch-at" }))
        : field("Time", h("input", { name: "time", type: "time", required: true, value: "02:00", "data-key": "sch-time" })),
      draft.repeat === "weekly" ? h("fieldset", {}, h("legend", {}, "Days"), DAY_NAMES.map((n, i) => h("label", { class: "check" },
        h("input", { type: "checkbox", name: `day${i}` }), n))) : null,
      h("button", { class: "btn quiet", type: "submit" }, "Add schedule"),
      h("small", { class: "src" }, `Times are in ${browserZone()}.`)) : null);
}

async function setBaseline(scenarioId, runId) {
  scenarioError("");
  try {
    await api(`/scenarios/${scenarioId}/baseline`, { method: "PUT", body: JSON.stringify({ run_id: runId }) });
    state.trends = await api(`/scenarios/${scenarioId}/trends`);
  } catch (e) { scenarioError(`Baseline not saved: ${e.message}`); return; }
  renderScenario();
}

function trendRow(s, t, r) {
  const isBaseline = r.id === t.baseline_run_id;
  return h("tr", {},
    h("td", {}, h("a", { href: `#/scenarios/${s.id}/run/${r.id}` }, localTime(r.created_at))),
    h("td", {}, h("span", { class: `badge ${r.verdict === "pass" ? "completed" : "failed"}` }, r.verdict === "pass" ? "passed" : "failed SLA"),
      r.status === "stopped" ? h("small", { class: "src" }, "stopped early") : null),
    h("td", { class: "num" }, fmt(r.users)), h("td", { class: "num" }, fmt(r.requests)),
    h("td", { class: "num" }, `${fmt(r.error_rate_percent, 2)}%`), h("td", { class: "num" }, `${fmt(r.avg_ms)} ms`),
    h("td", { class: "num" }, `${fmt(r.p95_ms)} ms`), h("td", { class: "num" }, `${r.sla_total - r.sla_failed} of ${r.sla_total}`),
    h("td", {}, isBaseline
      ? [h("strong", {}, "Baseline "), can("tester") ? h("button", { class: "linklike", type: "button", onclick: () => setBaseline(s.id, null) }, "clear") : null]
      : can("tester") ? h("button", { class: "btn quiet small", type: "button", onclick: () => setBaseline(s.id, r.id) }, "Use as baseline") : null));
}

function trendsSection(s) {
  const t = state.trends;
  if (!t || !t.runs.length) {
    return h("div", { class: "section" }, h("h2", {}, "Trends"), h("p", { class: "empty" }, "Trends appear once a run of this scenario finishes."));
  }
  const vs = t.vs_baseline;
  const worse = vs ? vs.rows.filter(r => r.verdict === "slower" || r.verdict === "more errors") : [];
  const change = (r) => r.change_percent == null ? "" : `, 90th percentile ${r.change_percent > 0 ? "+" : ""}${r.change_percent}%`;
  return h("div", { class: "section" }, h("h2", {}, "Trends"),
    h("p", { class: "intro" }, "Every finished run of this scenario. Choose a baseline and the latest run is checked against it: a business function regresses when its 90th percentile is more than 10% and 50 ms slower, or its error rate rises by more than one point."),
    t.runs.length > 1 && t.transactions.length ? h("div", { class: "hero" }, h("h2", {}, "90th percentile per business function, run by run (ms)"),
      h("div", { class: "chartbox small" }, h("canvas", { id: "chart-trend", role: "img",
        "aria-label": "90th percentile of each business function across runs; the table below has the numbers" }))) : null,
    vs ? (worse.length
      ? h("div", { class: "warn critical" }, `The latest run is worse than the baseline: ${worse.map(r => `${r.name} (${r.verdict}${change(r)})`).join("; ")}.`)
      : h("div", { class: "notice good" }, "The latest run is in line with the baseline.")) : null,
    h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["Run", "Result", "Users", "Requests", "Errors", "Average", "95th percentile", "SLAs met", "Baseline"]
        .map(x => h("th", { scope: "col" }, x)))),
      h("tbody", {}, t.runs.slice().reverse().map(r => trendRow(s, t, r))))));
}

function drawTrend() {
  const t = state.trends, el = document.getElementById("chart-trend");
  if (!t || !el || typeof Chart === "undefined") return;
  state.charts.trend = new Chart(el, { type: "line",
    data: { labels: t.runs.map(r => localTime(r.created_at)),
      datasets: t.transactions.map((x, i) => ({ label: x.name, data: x.points.map(p => (p ? p.p90 : null)), spanGaps: true,
        borderColor: TREND_COLORS[i % TREND_COLORS.length], backgroundColor: TREND_COLORS[i % TREND_COLORS.length], pointRadius: 3, borderWidth: 2 })) },
    options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { position: "bottom" } },
      scales: { y: { beginAtZero: true, title: { display: true, text: "ms" } } } } });
}
