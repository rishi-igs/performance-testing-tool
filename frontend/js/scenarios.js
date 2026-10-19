"use strict";
// Scenarios: groups of users running scripts on a schedule (LoadRunner's Controller).

const SLA_METRICS = [["p90", "90th percentile (ms)"], ["p95", "95th percentile (ms)"], ["p99", "99th percentile (ms)"],
  ["avg", "Average (ms)"], ["max", "Maximum (ms)"], ["error_rate", "Error rate (%)"], ["tps_min", "At least, per second"]];
const SPEEDS = [["", "Unlimited"], ["128", "128 kbps"], ["512", "512 kbps"], ["1000", "1 Mbps"], ["2000", "2 Mbps"],
  ["10000", "10 Mbps"], ["100000", "100 Mbps"]];

const blankSchedule = () => ({ start_delay_seconds: 0, ramp_up_seconds: 30, duration_seconds: 300, ramp_down_seconds: 0, iterations: null });
const newScenario = () => ({ name: "New scenario", mode: "manual", distribution: "users", total_users: null,
  schedule: blankSchedule(), groups: [], rendezvous: [], sla: [], goal: null, bandwidth_kbps: null,
  generators: [], monitors: [] });

async function refreshScenarios() {
  state.scenarios = await api(inProject("/scenarios"));
  const box = $("#scenarios");
  if (!state.scenarios.length) { box.replaceChildren(h("p", { class: "empty" }, "No scenarios yet.")); return; }
  box.replaceChildren(...state.scenarios.map(s => h("a", {
      class: "run", href: `#/scenarios/${s.id}`, "aria-current": s.id === state.scenarioId ? "page" : null },
    h("div", { class: "name" }, s.name),
    h("div", { class: "sub" }, `${s.groups} group${s.groups === 1 ? "" : "s"}${s.mode === "goal" ? " · goal" : ""} · ${ago(s.updated_at)}`))));
}

// Business functions of the scripts a scenario uses (for rendezvous and SLAs).
async function loadScriptInfo(ids) {
  state.scriptInfo = state.scriptInfo || {};
  for (const id of ids) {
    if (!id || state.scriptInfo[id]) continue;
    try { const s = await api(`/scripts/${id}`); state.scriptInfo[id] = { name: s.name, transactions: s.transactions.map(t => t.name) }; }
    catch (_) { state.scriptInfo[id] = { name: id, transactions: [] }; }
  }
}

async function openScenario(id) {
  clearTimeout(state.timer);
  state.scenarioId = id === "new" ? null : id;
  if (!state.scripts.length) await refreshScripts();
  try {
    state.schedules = []; state.trends = null;
    if (id === "new") {
      state.scenario = null;
      state.draft = newScenario();
      if (state.scripts[0]) state.draft.groups.push({ name: "Group 1", script_id: state.scripts[0].id, users: 10, percent: null, schedule: null, settings: null, enabled: true });
    } else {
      state.scenario = await api(`/scenarios/${id}`);
      state.draft = JSON.parse(JSON.stringify(state.scenario.config));
      await loadSchedulesAndTrends(id);
    }
  } catch (e) { setMain(h("div", { class: "notice" }, e.message)); return; }
  refreshScenarios();
  await loadScriptInfo(state.draft.groups.map(g => g.script_id));
  await loadInfra().catch(() => { state.infra = { generators: [], monitors: [] }; });
  state.preview = null;
  renderScenario();
  if (state.scenarioId) loadPreview();
}

async function loadPreview() {
  try { state.preview = await api(`/scenarios/${state.scenarioId}/preview`); }
  catch (e) { state.preview = { error: e.message }; }
  if (state.section === "scenarios") { renderScenario(focusedKey()); }
}

function scenarioError(text) { const el = $("#scenarioerror"); if (el) el.textContent = text; }

async function saveScenario() {
  scenarioError("");
  const body = JSON.stringify(state.draft);
  try {
    const saved = state.scenarioId
      ? await api(`/scenarios/${state.scenarioId}`, { method: "PUT", body })
      : await api(inProject("/scenarios"), { method: "POST", body });
    state.scenario = saved; state.scenarioId = saved.id;
    state.draft = JSON.parse(JSON.stringify(saved.config));
    await refreshScenarios();
    if (location.hash !== `#/scenarios/${saved.id}`) { location.hash = `#/scenarios/${saved.id}`; return true; }
    renderScenario(); loadPreview();
    return true;
  } catch (e) { scenarioError(`Not saved: ${e.message}`); return false; }
}

async function runScenario() {
  if (!(await saveScenario())) return;
  try {
    const run = await api(`/scenarios/${state.scenarioId}/run`, { method: "POST" });
    location.hash = `#/scenarios/${state.scenarioId}/run/${run.id}`;
  } catch (e) { scenarioError(`Not started: ${e.message}`); }
}

async function deleteScenario() {
  if (!state.scenarioId || !confirm(`Delete the scenario "${state.draft.name}"? Its past runs are kept.`)) return;
  try { await api(`/scenarios/${state.scenarioId}`, { method: "DELETE" }); } catch (e) { scenarioError(e.message); return; }
  state.scenarioId = null; await refreshScenarios(); go("#/scenarios");
}

function renderScenariosHome() {
  state.scenarioId = null;
  refreshScenarios();
  $("#main").replaceChildren(h("div", { class: "welcome" },
    h("h1", {}, "Scenarios"),
    h("p", {}, "A scenario decides who runs what: groups of virtual users, each running one of your scripts on a schedule (start delay, ramp-up, steady load, ramp-down)."),
    h("ol", { class: "steps" },
      h("li", {}, "Add a group per kind of user, for example 80 shoppers and 20 browsers."),
      h("li", {}, "Set the schedule, rendezvous points where users act at the same moment, and SLAs that decide pass or fail."),
      h("li", {}, "Or make it goal-oriented: hold a number of transactions or requests per second."),
      h("li", {}, "Run it and watch each business function live.")),
    h("p", {}, h("a", { class: "btn", href: "#/scenarios/new" }, "New scenario"))));
}

// ---- editor -----------------------------------------------------------------------------

const num = (v) => (v === "" || v == null ? null : Number(v));
function bind(obj, key, transform = (v) => v) {
  return (e) => { obj[key] = transform(e.target.type === "checkbox" ? e.target.checked : e.target.value); };
}
function rerender(fn) { return (e) => { fn(e); renderScenario(focusedKey()); }; }

function scheduleFields(sch, prefix) {
  const iter = sch.iterations != null;
  return h("div", { class: "schedule" },
    field("Start after (s)", h("input", { type: "number", min: 0, value: sch.start_delay_seconds, "data-key": `${prefix}-delay`,
      oninput: bind(sch, "start_delay_seconds", Number) })),
    field("Ramp-up (s)", h("input", { type: "number", min: 0, value: sch.ramp_up_seconds, "data-key": `${prefix}-ramp`,
      oninput: bind(sch, "ramp_up_seconds", Number) })),
    iter ? field("Iterations per user", h("input", { type: "number", min: 1, value: sch.iterations, "data-key": `${prefix}-iter`,
      oninput: bind(sch, "iterations", Number) }))
      : field("Steady load (s)", h("input", { type: "number", min: 1, value: sch.duration_seconds, "data-key": `${prefix}-hold`,
        oninput: bind(sch, "duration_seconds", Number) })),
    iter ? null : field("Ramp-down (s)", h("input", { type: "number", min: 0, value: sch.ramp_down_seconds, "data-key": `${prefix}-down`,
      oninput: bind(sch, "ramp_down_seconds", Number) })),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: iter, "data-key": `${prefix}-itermode`,
      onchange: rerender((e) => { sch.iterations = e.target.checked ? 1 : null; if (e.target.checked) sch.ramp_down_seconds = 0; }) }),
      "Run a number of iterations instead"));
}

function groupsTable(d) {
  const percent = d.distribution === "percent";
  const scripts = state.scripts.map(s => [s.id, s.name]);
  return h("div", { class: "tablewrap" }, h("table", { class: "grid" },
    h("thead", {}, h("tr", {}, ["On", "Group", "Script", percent ? "Share (%)" : (d.mode === "goal" ? "Weight" : "Users"), "Schedule", "Think time", ""]
      .map(t => h("th", { scope: "col" }, t)))),
    h("tbody", {}, d.groups.flatMap((g, n) => [
      h("tr", {},
        h("td", {}, h("input", { type: "checkbox", checked: g.enabled, "aria-label": `Run group ${g.name}`, onchange: bind(g, "enabled") })),
        h("td", {}, h("input", { value: g.name, maxlength: 60, "aria-label": "Group name", "data-key": `g-name-${n}`, oninput: bind(g, "name") })),
        h("td", {}, choice("script", scripts, g.script_id, { "aria-label": `Script for ${g.name}`,
          onchange: async (e) => { g.script_id = e.target.value; await loadScriptInfo([g.script_id]); renderScenario(); } })),
        h("td", {}, percent
          ? h("input", { type: "number", min: 0.1, max: 100, step: "any", class: "short", value: g.percent ?? "", "aria-label": `Share of users for ${g.name}`, oninput: bind(g, "percent", num) })
          : h("input", { type: "number", min: 1, class: "short", value: g.users, "aria-label": `Users in ${g.name}`, oninput: bind(g, "users", Number) })),
        h("td", {}, d.mode === "goal" ? "Scenario" : choice("sched", [["scenario", "Scenario schedule"], ["own", "Its own schedule"]],
          g.schedule ? "own" : "scenario", { "aria-label": `Schedule of ${g.name}`,
          onchange: rerender((e) => { g.schedule = e.target.value === "own" ? JSON.parse(JSON.stringify(d.schedule)) : null; }) })),
        h("td", {}, choice("think", [["script", "As in the script"], ["ignore", "None"]],
          g.settings && g.settings.think_time && g.settings.think_time.mode === "ignore" ? "ignore" : "script",
          { "aria-label": `Think time of ${g.name}`, onchange: (e) => { g.settings = e.target.value === "ignore" ? { think_time: { mode: "ignore" } } : null; } })),
        h("td", { class: "rowactions" }, h("button", { class: "btn quiet small", type: "button", "aria-label": `Remove group ${g.name}`,
          onclick: rerender(() => d.groups.splice(n, 1)) }, "Remove"))),
      g.schedule ? h("tr", { class: "detailrow" }, h("td", { colspan: 7 }, scheduleFields(g.schedule, `g${n}`))) : null,
    ]))));
}

function transactionOptions(groupName, d) {
  const g = d.groups.find(x => x.name === groupName);
  const info = g && state.scriptInfo[g.script_id];
  return (info ? info.transactions : []).map(t => [t, t]);
}
function allTransactions(d) {
  return [...new Set(d.groups.flatMap(g => (state.scriptInfo[g.script_id] || { transactions: [] }).transactions))];
}

function rendezvousSection(d) {
  return h("div", { class: "section" }, h("h2", {}, "Rendezvous points"),
    h("p", { class: "intro" }, "Users of a group wait for each other before a business function, then act at the same moment (for example, everyone pays at once)."),
    d.rendezvous.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["Name", "Group", "Before", "Wait for (% of users)", "Give up after (s)", ""].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, d.rendezvous.map((r, n) => h("tr", {},
        h("td", {}, h("input", { value: r.name, "aria-label": "Rendezvous name", oninput: bind(r, "name") })),
        h("td", {}, choice("rgroup", d.groups.map(g => [g.name, g.name]), r.group, { "aria-label": "Group",
          onchange: rerender((e) => { r.group = e.target.value; r.transaction = (transactionOptions(r.group, d)[0] || [""])[0]; }) })),
        h("td", {}, choice("rtx", transactionOptions(r.group, d), r.transaction, { "aria-label": "Business function", onchange: bind(r, "transaction") })),
        h("td", {}, h("input", { type: "number", min: 1, max: 100, class: "short", value: r.percent, "aria-label": "Percent of users", oninput: bind(r, "percent", Number) })),
        h("td", {}, h("input", { type: "number", min: 0, class: "short", value: r.timeout_seconds, "aria-label": "Timeout seconds", oninput: bind(r, "timeout_seconds", Number) })),
        h("td", { class: "rowactions" }, h("button", { class: "btn quiet small", type: "button", onclick: rerender(() => d.rendezvous.splice(n, 1)) }, "Remove"))))))) : null,
    h("button", { class: "btn quiet small", type: "button", disabled: !d.groups.length, onclick: rerender(() => {
      const g = d.groups[0];
      d.rendezvous.push({ name: `rendezvous ${d.rendezvous.length + 1}`, group: g.name,
        transaction: (transactionOptions(g.name, d)[0] || [""])[0], percent: 100, timeout_seconds: 30 }); }) }, "Add a rendezvous point"));
}

function slaSection(d) {
  const txs = [["*", "Every business function"], ...allTransactions(d).map(t => [t, t])];
  return h("div", { class: "section" }, h("h2", {}, "Service level agreements"),
    h("p", { class: "intro" }, "Limits that decide whether the run passes, checked per business function."),
    d.sla.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["Business function", "Measure", "Limit", ""].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, d.sla.map((r, n) => h("tr", {},
        h("td", {}, choice("stx", txs, r.transaction, { "aria-label": "Business function", onchange: bind(r, "transaction") })),
        h("td", {}, choice("smetric", SLA_METRICS, r.metric, { "aria-label": "Measure", onchange: bind(r, "metric") })),
        h("td", {}, h("input", { type: "number", min: 0, step: "any", class: "short", value: r.limit, "aria-label": "Limit", oninput: bind(r, "limit", Number) })),
        h("td", { class: "rowactions" }, h("button", { class: "btn quiet small", type: "button", onclick: rerender(() => d.sla.splice(n, 1)) }, "Remove"))))))) : null,
    h("button", { class: "btn quiet small", type: "button", onclick: rerender(() => d.sla.push({ transaction: "*", metric: "p90", limit: 2000 })) }, "Add an SLA"));
}

function previewChart() {
  const p = state.preview;
  if (!p) return h("p", { class: "empty" }, state.scenarioId ? "Loading the schedule…" : "Save the scenario to see its schedule.");
  if (p.error) return h("div", { class: "warn critical" }, p.error);
  return [
    h("div", { class: "hero" }, h("h2", {}, `Planned users${p.iterations ? " (iterations: shown until every user would have started)" : ""}`),
      h("div", { class: "chartbox small" }, h("canvas", { id: "chart-plan" }))),
    p.warnings.map(w => h("div", { class: "warn" }, w)),
    h("p", { class: "empty" }, `${fmt(p.total_users)} users at most · ${fmt(p.total_seconds)} s · calls ${p.hosts.join(", ") || "no hosts"}`),
  ];
}

function drawPlan() {
  const p = state.preview;
  const el = document.getElementById("chart-plan");
  if (!p || p.error || !el || typeof Chart === "undefined") return;
  const colors = ["#0f6e8c", "#b3261e", "#1f7a4d", "#a66a00", "#5b3b8c", "#16222e"];
  const names = Object.keys(p.groups);
  const labels = (p.groups[names[0]] || []).map(x => `${x.t}s`);
  state.charts.plan = new Chart(el, { type: "line", data: { labels, datasets: names.map((n, i) => ({
      label: n, data: p.groups[n].map(x => x.users), stepped: true, borderColor: colors[i % colors.length],
      backgroundColor: colors[i % colors.length], pointRadius: 0, borderWidth: 2 })) },
    options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { position: "bottom" } },
      scales: { x: { ticks: { maxTicksLimit: 12 } }, y: { beginAtZero: true, title: { display: true, text: "Users" } } } } });
}

function renderScenario(focusKey) {
  const d = state.draft, s = state.scenario;
  const scrollY = window.scrollY;
  clearCharts();
  setMain(
    h("div", { class: "head" },
      h("div", {}, h("h1", {}, d.name || "Scenario"),
        h("p", { class: "verdict" }, `${d.groups.length} group${d.groups.length === 1 ? "" : "s"} · ${d.mode === "goal" ? "goal-oriented" : "manual"}`)),
      h("div", { class: "actions" },
        h("button", { class: "btn", onclick: runScenario, disabled: !d.groups.length || !can("tester") }, "Save and run"),
        h("button", { class: "btn quiet", onclick: saveScenario, disabled: !can("tester") }, "Save"),
        s ? h("a", { class: "btn quiet", href: `/scenarios/${s.id}/export-jmx` }, "Download plan") : null,
        s && can("tester") ? h("button", { class: "btn quiet", onclick: deleteScenario }, "Delete") : null)),
    h("div", { class: "formerror", id: "scenarioerror", role: "alert" }),
    !can("tester") ? h("p", { class: "notice" }, "You can look at this scenario; changing and running it needs the tester role.") : null,
    !state.scripts.length ? h("div", { class: "warn" }, "Import a script first: a group runs one of your scripts.") : null,

    h("div", { class: "section" }, h("h2", {}, "Basics"),
      h("div", { class: "row" },
        field("Name", h("input", { value: d.name, maxlength: 100, "data-key": "sc-name", oninput: bind(d, "name") })),
        field("Network speed per user", choice("speed", SPEEDS, d.bandwidth_kbps ?? "", { onchange: bind(d, "bandwidth_kbps", num) }))),
      h("div", { class: "row" },
        field("Type", choice("mode", [["manual", "Manual: users and schedule"], ["goal", "Goal-oriented: hold a throughput"]], d.mode,
          { "data-key": "sc-mode", onchange: rerender((e) => { d.mode = e.target.value;
            if (d.mode === "goal") { d.goal = d.goal || { type: "transactions_per_second", target: 10, max_users: 50 };
              d.groups.forEach(g => { g.schedule = null; }); d.schedule.ramp_down_seconds = 0; d.schedule.iterations = null; d.distribution = "users"; }
            else d.goal = null; }) })),
        d.mode === "manual" ? field("Users are given", choice("dist", [["users", "Per group"], ["percent", "As a share of a total"]], d.distribution,
          { "data-key": "sc-dist", onchange: rerender((e) => { d.distribution = e.target.value; if (d.distribution === "percent") { d.total_users = d.total_users || 100;
            d.groups.forEach(g => { g.percent = g.percent || Math.round(100 / d.groups.length); }); } }) })) : h("span"))),
    d.mode === "manual" && d.distribution === "percent" ? field("Total users", h("input", { type: "number", min: 1, value: d.total_users ?? "",
      class: "short", oninput: bind(d, "total_users", num) })) : null,
    d.mode === "goal" ? h("div", { class: "section" }, h("h2", {}, "Goal"),
      h("div", { class: "row" },
        field("Hold", choice("gtype", [["transactions_per_second", "Business functions per second"], ["hits_per_second", "Requests per second"]],
          d.goal.type, { onchange: bind(d.goal, "type") })),
        field("Target per second", h("input", { type: "number", min: 0.1, step: "any", value: d.goal.target, oninput: bind(d.goal, "target", Number) }))),
      field("With at most this many users", h("input", { type: "number", min: 1, class: "short", value: d.goal.max_users, oninput: bind(d.goal, "max_users", Number) }),
        "Shared out by each group's weight. Use no think time, so throughput is not capped by pauses.")) : null,

    h("div", { class: "section" }, h("h2", {}, "Schedule"), scheduleFields(d.schedule, "s")),
    h("div", { class: "section" }, h("h2", {}, "Groups"), groupsTable(d),
      h("button", { class: "btn quiet small", type: "button", disabled: !state.scripts.length, onclick: async () => {
        const script = state.scripts[0];
        d.groups.push({ name: `Group ${d.groups.length + 1}`, script_id: script.id, users: 10,
          percent: d.distribution === "percent" ? 10 : null, schedule: null, settings: null, enabled: true });
        await loadScriptInfo([script.id]); renderScenario(); } }, "Add a group")),
    d.mode === "manual" ? rendezvousSection(d) : null,
    slaSection(d),
    infraPicker(d),
    h("div", { class: "section" }, h("h2", {}, "Schedule preview"), previewChart()),
    s && s.runs.length ? h("div", { class: "section" }, h("h2", {}, "Runs"),
      h("div", { class: "chips" }, s.runs.map(r => h("a", { class: "chip", href: `#/scenarios/${s.id}/run/${r.id}` },
        h("span", { class: `badge ${r.status === "running" ? "running" : r.verdict === "pass" ? "completed" : r.status === "stopped" ? "stopped" : "failed"}` },
          r.status === "running" ? "running" : r.status === "completed" ? (r.verdict === "pass" ? "passed" : "failed SLA") : r.status),
        ` ${ago(r.created_at)}`)))) : null,
    s ? schedulesSection(s) : null,
    s ? trendsSection(s) : null);
  drawPlan();
  drawTrend();
  window.scrollTo(0, scrollY);
  restoreFocus(focusKey);
}
