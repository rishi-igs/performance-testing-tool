"use strict";
// Analysis of a finished run (LoadRunner Analysis): overlay any graphs, error statistics,
// comparison with another run, and reports to download.

const CATEGORY_ORDER = ["Load", "Response time", "Business functions", "Throughput", "Errors", "Servers", "Requests"];
const DEFAULT_SERIES = ["users", "rt:p90"];

async function loadAnalysis(runId, bucket) {
  if (state.analysis && state.analysis.runId === runId && (state.analysis.data || state.analysis.loading)) return;
  state.analysis = { runId, loading: true, data: null, selected: new Set(DEFAULT_SERIES), compareWith: "" };
  try {
    state.analysis.data = await api(`/tests/${runId}/analysis?bucket_seconds=${bucket}`);
    const ids = new Set(state.analysis.data.series.map(s => s.id));
    const firstTx = state.analysis.data.series.find(s => s.id.startsWith("tx:") && s.id.endsWith(":p90"));
    state.analysis.selected = new Set(["users", firstTx ? firstTx.id : "rt:p90"].filter(id => ids.has(id)));
  } catch (e) { state.analysis.error = e.message; }
  state.analysis.loading = false;
}

function compareTargets(run) {
  if (run.kind === "scenario") return (state.scenario && state.scenario.runs || []).filter(r => r.id !== run.id && r.status !== "running");
  return state.runs.filter(r => r.id !== run.id && r.status !== "running" && (r.kind || "quick") === "quick");
}

function analysisSection(run, rerender) {
  const a = state.analysis;
  if (!a || a.runId !== run.id || a.loading) return h("div", { class: "section" }, h("h2", {}, "Analysis"), h("p", { class: "empty" }, "Loading the analysis…"));
  if (a.error) return h("div", { class: "section" }, h("h2", {}, "Analysis"), h("div", { class: "warn" }, a.error));
  const series = a.data.series;
  const byCategory = {};
  for (const s of series) (byCategory[s.category] ||= []).push(s);
  const toggle = (id) => (e) => { if (e.target.checked) a.selected.add(id); else a.selected.delete(id); rerender(`a-${id}`); };
  const targets = compareTargets(run);
  const compareHref = (other) => run.kind === "scenario"
    ? `#/scenarios/${state.scenarioId}/compare/${run.id}/${other}` : `#/quick/${run.id}/compare/${other}`;
  return h("div", { class: "section analysis" },
    h("h2", {}, "Analysis"),
    h("p", { class: "intro" }, "Tick any graphs to lay them over each other, for example running users against a business function's 90th percentile. Two units share the chart: the first on the left axis, the others on the right."),
    h("div", { class: "graphbuilder" },
      h("div", { class: "serieslist" }, CATEGORY_ORDER.filter(c => byCategory[c]).map(c =>
        h("details", { open: ["Load", "Response time", "Business functions"].includes(c) },
          h("summary", {}, `${c} (${byCategory[c].length})`),
          byCategory[c].map(s => h("label", { class: "check" }, h("input", { type: "checkbox", checked: a.selected.has(s.id),
            "data-key": `a-${s.id}`, onchange: toggle(s.id) }), s.label))))),
      h("div", { class: "hero" }, h("div", { class: "chartbox" }, h("canvas", { id: "chart-analysis" }),
        a.selected.size ? null : h("div", { class: "nodata" }, "Tick a graph to show it")))),
    a.data.errors.length ? h("div", { class: "section" }, h("h2", {}, "Errors"),
      h("div", { class: "tablewrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["Code", "Message", "Count", "First at", "Requests"].map(t => h("th", { scope: "col" }, t)))),
        h("tbody", {}, a.data.errors.map(x => h("tr", {},
          h("td", { class: "mono" }, x.code), h("td", {}, x.message), h("td", { class: "num" }, fmt(x.count)),
          h("td", { class: "num" }, `${x.first_seconds} s`), h("td", {}, x.requests.map(q => `${q.label} (${q.count})`).join(", ")))))))) : null,
    h("div", { class: "section" }, h("h2", {}, "Compare with another run"),
      targets.length ? h("form", { class: "inline", onsubmit: (e) => { e.preventDefault(); const v = e.target.elements.other.value; if (v) location.hash = compareHref(v); } },
        field("Baseline is this run; compare with", choice("other", targets.map(r => [r.id, `${r.name || run.name} · ${ago(r.created_at)}`]), targets[0].id)),
        h("button", { class: "btn quiet", type: "submit" }, "Compare"))
        : h("p", { class: "empty" }, "Run it again to compare two runs.")),
    h("div", { class: "section" }, h("h2", {}, "Reports"),
      h("div", { class: "links" },
        h("a", { class: "btn quiet", href: `/reports/${run.id}/summary.html`, target: "_blank", rel: "noopener" }, "Open report (print to PDF)"),
        h("a", { class: "btn quiet", href: `/reports/${run.id}/summary.pdf` }, "PDF"),
        h("a", { class: "btn quiet", href: `/reports/${run.id}/summary.docx` }, "Word"))));
}

function drawAnalysis() {
  const a = state.analysis, el = document.getElementById("chart-analysis");
  if (!a || !a.data || !el || typeof Chart === "undefined") return;
  const chosen = a.data.series.filter(s => a.selected.has(s.id));
  if (!chosen.length) return;
  const units = [...new Set(chosen.map(s => s.unit))];
  const ts = [...new Set(chosen.flatMap(s => s.points.map(p => p.t)))].sort((x, y) => x - y);
  const datasets = chosen.map((s, i) => {
    const at = new Map(s.points.map(p => [p.t, p.v]));
    return { label: s.label, data: ts.map(t => (at.has(t) ? at.get(t) : null)), yAxisID: s.unit === units[0] ? "y" : "y2",
      borderColor: PALETTE[i % PALETTE.length], backgroundColor: PALETTE[i % PALETTE.length], pointRadius: 0,
      borderWidth: 2, spanGaps: true, stepped: s.unit === "users", tension: s.unit === "users" ? 0 : .2 };
  });
  state.charts.analysis = new Chart(el, { type: "line", data: { labels: ts.map(t => `${t}s`), datasets },
    options: { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } } },
      scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 12 } },
        y: { beginAtZero: true, title: { display: true, text: units[0] } },
        ...(units.length > 1 ? { y2: { position: "right", beginAtZero: true, grid: { display: false },
          title: { display: true, text: units.slice(1).join(", ") } } } : {}) } } });
}

// ---- comparison of two runs ------------------------------------------------------------

async function openCompare(a, b, back) {
  clearTimeout(state.timer);
  setMain(h("p", { class: "empty" }, "Comparing…"));
  let result;
  try { result = await api(`/tests/${a}/compare/${b}?bucket_seconds=5`); }
  catch (e) { setMain(h("div", { class: "notice" }, e.message)); return; }
  renderCompare(result, back);
}

function changeCell(m, lowerIsBetter = true) {
  if (m.change_percent == null) return h("td", { class: "num" }, "–");
  const worse = lowerIsBetter ? m.change_percent > 0 : m.change_percent < 0;
  return h("td", { class: `num ${Math.abs(m.change_percent) < 5 ? "" : worse ? "worse" : "better"}` },
    `${m.change_percent > 0 ? "+" : ""}${fmt(m.change_percent, 1)}%`);
}

function renderCompare(c, back) {
  clearCharts();
  const label = (r) => `${r.name} · ${new Date(r.started_at || r.created_at).toLocaleString()}`;
  setMain(
    h("p", {}, h("a", { href: back }, "← Back to the run")),
    h("h1", {}, "Run comparison"),
    h("p", { class: "url" }, "A (baseline): ", label(c.a), h("br"), "B: ", label(c.b)),
    h("p", { class: `verdict ${c.regressions ? "fail" : "pass"}` },
      c.regressions ? `${c.regressions} ${c.kind === "requests" ? "request" : "business function"}${c.regressions === 1 ? " got" : "s got"} worse in B.`
        : "No regressions: B is within 10% (or 50 ms) of A everywhere."),
    h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, [c.kind === "requests" ? "Request" : "Business function", "Count A", "Count B", "90% A", "90% B", "Change",
        "Avg A", "Avg B", "Change", "Errors A", "Errors B", "Per s A", "Per s B", ""].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, c.rows.map(r => h("tr", { class: r.verdict === "slower" || r.verdict === "more errors" ? "failrow" : null },
        h("td", {}, r.name, r.in !== "both" ? h("small", { class: "src" }, r.in === "a" ? "only in A" : "only in B") : null),
        h("td", { class: "num" }, fmt(r.count.a)), h("td", { class: "num" }, fmt(r.count.b)),
        h("td", { class: "num" }, r.p90.a == null ? "–" : `${fmt(r.p90.a)} ms`), h("td", { class: "num" }, r.p90.b == null ? "–" : `${fmt(r.p90.b)} ms`), changeCell(r.p90),
        h("td", { class: "num" }, r.avg.a == null ? "–" : `${fmt(r.avg.a, 1)} ms`), h("td", { class: "num" }, r.avg.b == null ? "–" : `${fmt(r.avg.b, 1)} ms`), changeCell(r.avg),
        h("td", { class: "num" }, r.error_rate_percent.a == null ? "–" : `${fmt(r.error_rate_percent.a, 2)}%`),
        h("td", { class: "num" }, r.error_rate_percent.b == null ? "–" : `${fmt(r.error_rate_percent.b, 2)}%`),
        h("td", { class: "num" }, fmt(r.tps.a, 2)), h("td", { class: "num" }, fmt(r.tps.b, 2)),
        h("td", {}, r.verdict ? h("span", { class: `badge ${r.verdict === "faster" ? "completed" : "failed"}` }, r.verdict) : "")))))),
    ["rt:p90", "users", "hits"].map(id => h("div", { class: "hero" },
      h("h2", {}, { "rt:p90": "90th percentile of all requests", users: "Running users", hits: "Hits per second" }[id]),
      h("div", { class: "chartbox small" }, h("canvas", { id: `chart-cmp-${id.replace(":", "-")}` })))));
  for (const id of ["rt:p90", "users", "hits"]) {
    const a = c.series.a[id] || [], b = c.series.b[id] || [];
    const ts = [...new Set([...a, ...b].map(p => p.t))].sort((x, y) => x - y);
    const pick = (points) => { const at = new Map(points.map(p => [p.t, p.v])); return ts.map(t => (at.has(t) ? at.get(t) : null)); };
    lineChart(`chart-cmp-${id.replace(":", "-")}`, ts.map(t => `${t}s`), [
      { label: "A (baseline)", data: pick(a), borderColor: PALETTE[0], backgroundColor: PALETTE[0], pointRadius: 0, borderWidth: 2, spanGaps: true },
      { label: "B", data: pick(b), borderColor: PALETTE[1], backgroundColor: PALETTE[1], pointRadius: 0, borderWidth: 2, spanGaps: true, borderDash: [5, 4] },
    ], id === "users" ? "users" : id === "hits" ? "/s" : "ms");
  }
}
