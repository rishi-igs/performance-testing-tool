"use strict";
// Overview: what is running now with live numbers, alerts from the latest runs, and recent results.

const runHref = (r) => (r.kind === "scenario" && r.scenario_id ? `#/scenarios/${r.scenario_id}/run/${r.id}` : `#/quick/${r.id}`);

function resultBadge(r) {
  if (r.status === "running") return h("span", { class: "badge running" }, "running");
  if (r.status === "completed" && r.verdict === "pass") return h("span", { class: "badge completed" }, "passed");
  if (r.status === "completed" && r.verdict === "fail") return h("span", { class: "badge failed" }, r.kind === "scenario" ? "failed SLA" : "failed limits");
  return h("span", { class: `badge ${r.status === "stopped" ? "stopped" : "failed"}` }, r.status);
}

const kindLabel = (r) => (r.kind === "scenario" ? "Scenario" : `Quick · ${TEST_TYPES[r.test_type || "load"] || "Load"}`);

async function openOverview() {
  clearTimeout(state.timer);
  let data;
  try { data = await api(inProject("/dashboard")); }
  catch (e) { setMain(h("div", { class: "notice" }, `Cannot load the overview: ${e.message}`)); return; }
  if (state.section !== "overview") return;
  renderOverview(data);
  if (data.running.length) state.timer = setTimeout(openOverview, 3000);
}

function renderOverview(d) {
  const scrollY = window.scrollY;
  clearCharts();
  const day = d.last_day;
  const progress = (r) => {
    if (!r.started_at || !r.duration_seconds) return null;
    const pct = Math.min(100, ((Date.now() - Date.parse(r.started_at)) / 1000) / r.duration_seconds * 100);
    return h("div", { class: "progress mini", role: "progressbar", "aria-valuenow": Math.round(pct), "aria-valuemin": 0,
      "aria-valuemax": 100, "aria-label": `${r.name}: ${Math.round(pct)}% of the planned time` }, h("div", { style: `width:${pct}%` }));
  };
  const numbers = (r) => [
    h("td", { class: "num" }, fmt(r.users)), h("td", { class: "num" }, fmt(r.requests)),
    h("td", { class: "num" }, r.error_rate_percent == null ? "–" : `${fmt(r.error_rate_percent, 2)}%`),
    h("td", { class: "num" }, r.p95_ms == null ? "–" : `${fmt(r.p95_ms)} ms`),
    h("td", { class: "num" }, r.throughput_rps == null ? "–" : fmt(r.throughput_rps, 1))];
  setMain(
    h("div", { class: "head" },
      h("div", {}, h("h1", {}, "Overview"),
        h("p", { class: "verdict" }, `Last 24 hours: ${fmt(day.runs)} run${day.runs === 1 ? "" : "s"} · ${fmt(day.passed)} passed · `
          + `${fmt(day.failed)} failed · ${fmt(day.running)} running`)),
      h("div", { class: "actions" },
        h("a", { class: "btn", href: "#/quick" }, "Quick test"),
        h("a", { class: "btn quiet", href: "#/scripts" }, `Scripts (${fmt(d.scripts)})`),
        h("a", { class: "btn quiet", href: "#/scenarios" }, `Scenarios (${fmt(d.scenarios)})`))),

    h("div", { class: "section" }, h("h2", {}, "Running now"),
      d.running.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["Run", "Kind", "Progress", "Users", "Requests", "Errors", "p95", "Per second"].map(t => h("th", { scope: "col" }, t)))),
        h("tbody", {}, d.running.map(r => h("tr", { class: (r.error_rate_percent || 0) > 5 ? "failrow" : null },
          h("td", {}, h("a", { href: runHref(r) }, r.name)), h("td", {}, kindLabel(r)), h("td", {}, progress(r)), ...numbers(r))))))
        : h("p", { class: "empty" }, "Nothing is running.")),

    h("div", { class: "section" }, h("h2", {}, "Alerts"),
      d.alerts.length ? d.alerts.map(a => h("div", { class: `warn ${a.severity}` },
        h("a", { href: runHref(a.run) }, a.run.name), a.run.finished_at ? ` · ${ago(a.run.finished_at)}` : "", ": ", a.message))
        : h("p", { class: "allclear" }, "No problems in the latest runs.")),

    h("div", { class: "section" }, h("h2", {}, "Recent results"),
      d.recent.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["Run", "Kind", "Result", "Users", "Requests", "Errors", "p95", "Per second", "Finished"].map(t => h("th", { scope: "col" }, t)))),
        h("tbody", {}, d.recent.map(r => h("tr", { class: r.verdict === "fail" || r.status === "failed" ? "failrow" : null },
          h("td", {}, h("a", { href: runHref(r) }, r.name)), h("td", {}, kindLabel(r)), h("td", {}, resultBadge(r)), ...numbers(r),
          h("td", {}, r.finished_at ? ago(r.finished_at) : "–"))))))
        : h("p", { class: "empty" }, "No runs yet. Start a quick test, or import a recording and build a scenario.")));
  window.scrollTo(0, scrollY);
}
