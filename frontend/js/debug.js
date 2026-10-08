"use strict";
// Debug replay: run the script once with one user, compare it with the recording, explain failures.

const FIX_TABS = { correlation: "Correlation", parameters: "Parameters", checks: "Checks", requests: "Requests" };

async function startDebug() {
  try {
    const run = await api(`/scripts/${state.script}/debug-run`, { method: "POST" });
    state.debugSample = null;
    go(`#/scripts/${state.script}/debug/${run.id}`);
  } catch (e) { showError(e.message); }
}

async function loadDebug(runId) {
  clearTimeout(state.timer);
  const script = state.script;
  try { state.debugRuns = await api(`/scripts/${script}/debug-runs`); } catch (_) { state.debugRuns = []; }
  const id = runId || (state.debugRuns[0] && state.debugRuns[0].id);
  state.debugRun = id ? await api(`/debug-runs/${id}`).catch(() => null) : null;
  if (state.debugSample && (!state.debugRun || state.debugSample.run !== state.debugRun.id)) state.debugSample = null;
  if (state.scriptTab !== "debug" || state.script !== script) return;     // the user moved on
  renderScriptPage();
  if (state.debugRun && state.debugRun.status === "running") state.timer = setTimeout(() => loadDebug(id), 1000);
}

async function openSample(index) {
  const run = state.debugRun;
  try {
    const detail = await api(`/debug-runs/${run.id}/samples/${index}${state.debugReveal ? "?reveal=true" : ""}`);
    state.debugSample = { run: run.id, index, detail };
  } catch (e) { state.debugSample = { run: run.id, index, error: e.message }; }
  renderScriptPage(`d-row-${index}`);
}

function prettyBody(text) {
  if (!text) return "";
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (_) { return text; }
}

function runBadge(r) {
  if (r.status === "running") return h("span", { class: "badge running" }, "running");
  if (r.status === "failed" && r.verdict == null) return h("span", { class: "badge failed" }, "error");
  return h("span", { class: `badge ${r.verdict === "pass" ? "completed" : "failed"}` }, r.verdict === "pass" ? "passed" : "failed");
}

function sampleDetail(row) {
  const sel = state.debugSample;
  if (!sel || sel.index !== row.index) return null;
  if (sel.error) return h("div", { class: "warn critical" }, sel.error);
  const d = sel.detail, dx = row.diagnosis;
  return h("div", { class: "detail", "aria-label": `Details of request #${row.index}` },
    dx ? h("div", { class: "diagnosis" },
      h("p", {}, h("strong", {}, "Likely cause: "), dx.cause),
      h("p", {}, h("strong", {}, "Fix: "), dx.fix, " ",
        FIX_TABS[dx.where] ? h("a", { href: `#/scripts/${state.script}/${dx.where}` }, `Open ${FIX_TABS[dx.where]}`) : null)) : null,
    h("div", { class: "pane" },
      h("h3", {}, "Request"),
      h("p", { class: "mono" }, `${d.method} ${d.url}`),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: state.debugReveal, "data-key": "d-reveal",
        onchange: (e) => { state.debugReveal = e.target.checked; openSample(row.index); } }), "Show credentials and cookies"),
      h("pre", { class: "code" }, d.request_headers || "(no headers)"),
      d.request_body ? h("pre", { class: "code" }, prettyBody(d.request_body)) : null),
    h("div", { class: "pane" },
      h("h3", {}, `Response: ${d.status} ${d.message}`),
      h("p", { class: "empty" }, `${fmt(d.elapsed_ms)} ms (connect ${fmt(d.connect_ms)} ms, first byte ${fmt(d.latency_ms)} ms) · ${fmtBytes(d.bytes)}`),
      h("pre", { class: "code" }, d.response_headers || "(no headers)"),
      h("pre", { class: "code body" }, prettyBody(d.response_body) || "(empty body)"),
      d.response_truncated ? h("p", { class: "empty" }, "The body was cut at 256 KB.") : null),
    d.assertions.length ? h("div", { class: "pane" }, h("h3", {}, "Checks"),
      h("ul", { class: "problems" }, d.assertions.map(a => h("li", { class: a.failed ? "bad" : "good" },
        a.failed ? "Failed: " : "Passed: ", a.name, a.failed && a.message ? ` (${a.message})` : "")))) : null);
}

function debugTab() {
  const run = state.debugRun, runs = state.debugRuns;
  const running = run && run.status === "running";
  const top = h("div", { class: "debugbar" },
    h("button", { class: "btn", onclick: startDebug, disabled: !state.scriptView.summary.kept || running },
      running ? "Replaying…" : "Run debug replay"),
    h("span", { class: "empty" }, "One user, one iteration, no think time or pacing. Every request and response is kept."));
  const history = runs.length ? h("div", { class: "chips", "aria-label": "Recent debug replays" }, runs.map(r =>
    h("a", { class: "chip", href: `#/scripts/${state.script}/debug/${r.id}`, "aria-current": run && r.id === run.id ? "page" : null },
      runBadge(r), ` ${ago(r.created_at)}`))) : null;
  if (!run) return [top, history, h("p", { class: "empty" }, "No replays yet.")];
  if (running) return [top, history, h("p", { class: "verdict" }, "Replaying the script once…")];
  const s = run.summary;
  if (!s) return [top, history, h("div", { class: "warn critical" }, run.error || "The replay produced no results.")];

  const first = s.first_failure != null ? s.items.find(i => i.index === s.first_failure) : null;
  const rows = s.items.filter(i => !state.debugFailedOnly || i.issues.length || !i.success);
  const statusCell = (i) => i.state === "not_run" ? h("span", { class: "badge stopped" }, "not run")
    : i.success && !i.issues.length ? h("span", { class: "badge completed" }, "OK") : h("span", { class: "badge failed" }, "failed");
  return [
    top, history,
    h("p", { class: `verdict ${s.verdict === "pass" ? "pass" : "fail"}` },
      s.verdict === "pass" ? `Passed: all ${s.requests} requests behaved as recorded.`
        : `${s.failed} of ${s.requests} requests failed${s.not_run ? `, ${s.not_run} did not run` : ""}.`,
      first ? [" First failure: ", h("button", { class: "linklike", onclick: () => openSample(first.index) }, `#${first.index} ${first.label}`), "."] : null),
    run.error ? h("div", { class: "warn" }, run.error) : null,
    (s.warnings || []).map(w => h("div", { class: "warn" }, w)),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: state.debugFailedOnly, "data-key": "d-failed",
      onchange: (e) => { state.debugFailedOnly = e.target.checked; renderScriptPage("d-failed"); } }), "Failed requests only"),
    h("div", { class: "tablewrap" }, h("table", { class: "reqs debug" },
      h("thead", {}, h("tr", {}, ["", "Request", "Business function", "Recorded", "Replay", "Time", "What happened"].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, rows.flatMap(i => [
        h("tr", { class: [i.index === s.first_failure ? "first" : "", i.issues.length || !i.success ? "failrow" : ""].join(" ").trim() || null },
          h("td", {}, statusCell(i)),
          h("td", {}, i.state === "ran"
            ? h("button", { class: "linklike", "data-key": `d-row-${i.index}`, "aria-expanded": String(!!(state.debugSample && state.debugSample.index === i.index)),
                onclick: () => (state.debugSample && state.debugSample.index === i.index ? (state.debugSample = null, renderScriptPage(`d-row-${i.index}`)) : openSample(i.index)) },
                `#${i.index} ${i.label}`)
            : `#${i.index} ${i.label}`),
          h("td", {}, i.transaction || ""),
          h("td", {}, i.recorded_status == null ? "–" : String(i.recorded_status)),
          h("td", {}, i.status || "–"),
          h("td", { class: "num" }, i.elapsed_ms == null ? "–" : `${fmt(i.elapsed_ms)} ms`),
          h("td", { class: "reason" }, i.issues.length ? h("ul", { class: "issues" }, i.issues.map(t => h("li", {}, t))) : "")),
        state.debugSample && state.debugSample.index === i.index
          ? h("tr", { class: "detailrow" }, h("td", { colspan: 7 }, sampleDetail(i))) : null,
      ])))),
    Object.keys(s.variables || {}).length ? h("div", { class: "section" }, h("h2", {}, "Values captured"),
      h("table", { class: "grid" }, h("tbody", {}, Object.entries(s.variables).map(([name, v]) => h("tr", { class: v.not_found ? "failrow" : null },
        h("td", { class: "mono" }, varRef(name)),
        h("td", { class: "mono" }, v.preview),
        h("td", {}, v.not_found ? "not found: check the correlation" : "")))))) : null,
  ];
}
