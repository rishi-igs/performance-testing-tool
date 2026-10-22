"use strict";
// Debug replay: run the script once with one user (or a few users or iterations), compare it with
// the recording, explain failures, and compare one replay with another.

const FIX_TABS = { correlation: "Correlation", parameters: "Parameters", checks: "Checks", requests: "Requests" };

async function startDebug() {
  const o = state.debugOpts;
  try {
    const run = await api(`/scripts/${state.script}/debug-run?users=${o.users}&iterations=${o.iterations}`, { method: "POST" });
    state.debugSample = null; state.debugSearch = null; state.debugCompare = null;
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
  if (state.debugSearch && (!state.debugRun || state.debugSearch.run !== state.debugRun.id)) state.debugSearch = null;
  if (state.debugCompare && (!state.debugRun || state.debugCompare.run !== state.debugRun.id)) state.debugCompare = null;
  if (state.scriptTab !== "debug" || state.script !== script) return;     // the user moved on
  renderScriptPage();
  if (state.debugRun && state.debugRun.status === "running") state.timer = setTimeout(() => loadDebug(id), 1000);
}

async function openSample(index, occurrence = null) {
  const run = state.debugRun;
  const row = run.summary.items.find(i => i.index === index);
  const n = occurrence ?? (row ? row.occurrence || 0 : 0);
  try {
    const detail = await api(`/debug-runs/${run.id}/samples/${index}?occurrence=${n}${state.debugReveal ? "&reveal=true" : ""}`);
    state.debugSample = { run: run.id, index, detail };
  } catch (e) { state.debugSample = { run: run.id, index, error: e.message }; }
  renderScriptPage(`d-row-${index}`);
}

async function searchDebug(q) {
  const run = state.debugRun;
  if (!q.trim()) { state.debugSearch = null; renderScriptPage("d-search"); return; }
  try {
    const found = await api(`/debug-runs/${run.id}/search?q=${encodeURIComponent(q)}${state.debugReveal ? "&reveal=true" : ""}`);
    state.debugSearch = { run: run.id, q, hits: found.hits };
  } catch (e) { state.debugSearch = { run: run.id, q, hits: [], error: e.message }; }
  renderScriptPage("d-search");
}

async function compareDebug(otherId) {
  const run = state.debugRun;
  if (!otherId) { state.debugCompare = null; renderScriptPage("d-compare"); return; }
  try { state.debugCompare = { run: run.id, other: await api(`/debug-runs/${otherId}`) }; }
  catch (e) { state.debugCompare = { run: run.id, error: e.message }; }
  renderScriptPage("d-compare");
}

// JSON, XML and HTML bodies are indented so they can be read.
function prettyBody(text) {
  if (!text) return "";
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (_) {}
  const t = text.trim();
  if (!t.startsWith("<") || t.length > 200000) return text;
  const lines = [];
  let depth = 0;
  for (const token of t.replace(/>\s*</g, ">\n<").split("\n")) {
    const closing = /^<\//.test(token), selfClosing = /\/>$/.test(token) || /^<(!|\?)/.test(token)
      || /^<(br|hr|img|input|meta|link|area|base|col|source|wbr)\b/i.test(token);
    const opensAndCloses = /^<([\w:-]+)[^>]*>.*<\/\1>$/.test(token);
    if (closing) depth = Math.max(0, depth - 1);
    lines.push("  ".repeat(depth) + token);
    if (!closing && !selfClosing && !opensAndCloses && token.startsWith("<")) depth++;
  }
  return lines.join("\n");
}

// Text with each match of the search marked.
function marked(text, q) {
  if (!q || !text) return text;
  const out = [], low = text.toLowerCase(), needle = q.toLowerCase();
  let at = 0, i;
  while ((i = low.indexOf(needle, at)) >= 0) {
    out.push(text.slice(at, i), h("mark", {}, text.slice(i, i + needle.length)));
    at = i + needle.length;
  }
  out.push(text.slice(at));
  return out;
}

function runBadge(r) {
  if (r.status === "running") return h("span", { class: "badge running" }, "running");
  if (r.status === "failed" && r.verdict == null) return h("span", { class: "badge failed" }, "error");
  return h("span", { class: `badge ${r.verdict === "pass" ? "completed" : "failed"}` }, r.verdict === "pass" ? "passed" : "failed");
}

// The link next to a diagnosis opens the editor that fixes it when it can.
function fixLink(dx) {
  if (!FIX_TABS[dx.where]) return null;
  if (dx.where === "correlation" && dx.variable) {
    const rules = state.scriptView.design.correlations;
    const n = rules.findIndex(c => c.variable === dx.variable);
    if (dx.editor === "edit" && n >= 0) {
      return h("button", { class: "linklike", onclick: () => { state.editing = { kind: "correlation", index: n, draft: {} };
        location.hash = `#/scripts/${state.script}/correlation`; } }, `Edit the correlation for ${varRef(dx.variable)}`);
    }
    return h("button", { class: "linklike", onclick: () => editIn("correlation", "correlation", { variable: dx.variable }) },
      `Add a correlation for ${varRef(dx.variable)}`);
  }
  return h("a", { href: `#/scripts/${state.script}/${dx.where}` }, `Open ${FIX_TABS[dx.where]}`);
}

function sampleDetail(row) {
  const sel = state.debugSample;
  if (!sel || sel.index !== row.index) return null;
  if (sel.error) return h("div", { class: "warn critical" }, sel.error);
  const d = sel.detail, dx = row.diagnosis, q = state.debugSearch && state.debugSearch.q;
  const captured = state.debugRun.summary.variables || {};
  return h("div", { class: "detail", "aria-label": `Details of request #${row.index}` },
    dx ? h("div", { class: "diagnosis" },
      h("p", {}, h("strong", {}, "Likely cause: "), dx.cause),
      h("p", {}, h("strong", {}, "Fix: "), dx.fix, " ", fixLink(dx))) : null,
    d.occurrences > 1 ? h("div", { class: "chips", "aria-label": "Results of this request" },
      h("span", { class: "empty" }, "Result: "),
      Array.from({ length: d.occurrences }, (_, n) => h("button", { class: "chip", "aria-current": n === d.occurrence ? "page" : null,
        onclick: () => openSample(row.index, n) }, `${n + 1}`))) : null,
    (row.uses || []).length ? h("div", { class: "pane" }, h("h3", {}, "Variables sent"),
      h("ul", { class: "issues" }, row.uses.map(name => h("li", {}, h("code", {}, varRef(name)),
        captured[name] ? ` = ${captured[name].preview}${captured[name].not_found ? " (not found)" : ""}` : "")))) : null,
    h("div", { class: "pane" },
      h("h3", {}, "Request"),
      h("p", { class: "mono" }, `${d.method} `, marked(d.url, q)),
      can("tester") ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: state.debugReveal, "data-key": "d-reveal",
        onchange: (e) => { state.debugReveal = e.target.checked; openSample(row.index, d.occurrence); } }), "Show credentials, tokens and cookies") : null,
      h("pre", { class: "code" }, marked(d.request_headers || "(no headers)", q)),
      d.request_body ? h("pre", { class: "code" }, marked(prettyBody(d.request_body), q)) : null),
    h("div", { class: "pane" },
      h("h3", {}, `Response: ${d.status} ${d.message}`),
      h("p", { class: "empty" }, `${fmt(d.elapsed_ms)} ms (connect ${fmt(d.connect_ms)} ms, first byte ${fmt(d.latency_ms)} ms) · ${fmtBytes(d.bytes)}`),
      h("pre", { class: "code" }, marked(d.response_headers || "(no headers)", q)),
      h("pre", { class: "code body" }, marked(prettyBody(d.response_body) || "(empty body)", q)),
      d.response_truncated ? h("p", { class: "empty" }, "The body was cut at 256 KB.") : null),
    d.assertions.length ? h("div", { class: "pane" }, h("h3", {}, "Checks"),
      h("ul", { class: "problems" }, d.assertions.map(a => h("li", { class: a.failed ? "bad" : "good" },
        a.failed ? "Failed: " : "Passed: ", a.name, a.failed && a.message ? ` (${a.message})` : "")))) : null);
}

function debugControls(running) {
  const o = state.debugOpts;
  const count = (label, key) => h("label", {}, label, h("input", { type: "number", min: 1, max: 5, value: o[key], class: "short",
    "data-key": `d-${key}`, oninput: (e) => { o[key] = Math.min(5, Math.max(1, parseInt(e.target.value, 10) || 1)); } }));
  return h("div", { class: "debugbar" },
    h("button", { class: "btn", onclick: startDebug, disabled: !state.scriptView.summary.kept || running || !can("tester") },
      running ? "Replaying…" : "Run debug replay"),
    count("Users", "users"), count("Iterations", "iterations"),
    h("span", { class: "empty" }, "No think time or pacing. Every request and response is kept (the last 10 replays). "
      + "A few users or iterations check each row of a data file."));
}

function compareSection(run) {
  const others = state.debugRuns.filter(r => r.id !== run.id && r.status !== "running");
  if (!others.length) return null;
  const cmp = state.debugCompare;
  const picker = h("div", { class: "toolbar" }, h("label", {}, "Compare with another replay", choice("other", [["", "Choose a replay"],
    ...others.map(r => [r.id, `${r.verdict === "pass" ? "passed" : "failed"} · ${ago(r.created_at)}`])],
    cmp && cmp.other ? cmp.other.id : "", { "data-key": "d-compare", onchange: (e) => compareDebug(e.target.value) })));
  if (!cmp || cmp.error || !cmp.other || !cmp.other.summary) {
    return h("div", { class: "section" }, picker, cmp && cmp.error ? h("div", { class: "warn" }, cmp.error) : null);
  }
  const theirs = new Map(cmp.other.summary.items.map(i => [i.index, i]));
  const state_ = (i) => (!i ? "–" : i.state === "not_run" ? "not run" : i.failed_runs ? "failed" : "OK");
  const rows = run.summary.items.map(i => ({ i, o: theirs.get(i.index) }));
  const changed = rows.filter(({ i, o }) => state_(i) !== state_(o)).length;
  return h("div", { class: "section" }, picker,
    h("p", { class: "verdict" }, changed ? `${changed} request${changed === 1 ? "" : "s"} behave differently.` : "Both replays behave the same."),
    h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["Request", "This replay", "The other replay", "What happened in the other"].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, rows.map(({ i, o }) => h("tr", { class: state_(i) !== state_(o) ? "failrow" : null },
        h("td", {}, `#${i.index} ${i.label}`), h("td", {}, state_(i)), h("td", {}, state_(o)),
        h("td", { class: "reason" }, o && o.issues.length ? o.issues.join("; ") : "")))))));
}

function debugTab() {
  const run = state.debugRun, runs = state.debugRuns;
  const running = run && run.status === "running";
  const top = debugControls(running);
  const history = runs.length ? h("div", { class: "chips", "aria-label": "Recent debug replays" }, runs.map(r =>
    h("a", { class: "chip", href: `#/scripts/${state.script}/debug/${r.id}`, "aria-current": run && r.id === run.id ? "page" : null },
      runBadge(r), ` ${ago(r.created_at)}${r.users > 1 || r.iterations > 1 ? ` · ${r.users}×${r.iterations}` : ""}`))) : null;
  if (!run) return [top, history, h("p", { class: "empty" }, "No replays yet.")];
  if (running) return [top, history, h("p", { class: "verdict" }, "Replaying the script…")];
  const s = run.summary;
  if (!s) return [top, history, h("div", { class: "warn critical" }, run.error || "The replay produced no results.")];

  const search = state.debugSearch;
  const hitIndexes = search ? new Set(search.hits.map(x => x.index)) : null;
  const first = s.first_failure != null ? s.items.find(i => i.index === s.first_failure) : null;
  const rows = s.items.filter(i => (!state.debugFailedOnly || i.issues.length || !i.success) && (!hitIndexes || hitIndexes.has(i.index)));
  const statusCell = (i) => i.state === "not_run" ? h("span", { class: "badge stopped" }, "not run")
    : !i.failed_runs ? h("span", { class: "badge completed" }, "OK") : h("span", { class: "badge failed" }, "failed");
  const several = (s.users || 1) > 1 || (s.iterations || 1) > 1;
  const v = s.validation;
  return [
    top, history,
    h("p", { class: `verdict ${s.verdict === "pass" ? "pass" : "fail"}` },
      s.verdict === "pass" ? `Passed: all ${s.requests} requests behaved as recorded${several ? ` in every run (${s.users} users × ${s.iterations} iterations)` : ""}.`
        : `${s.failed} of ${s.requests} requests failed${s.not_run ? `, ${s.not_run} did not run` : ""}.`,
      first ? [" First failure: ", h("button", { class: "linklike", onclick: () => openSample(first.index) }, `#${first.index} ${first.label}`), "."] : null),
    v ? h("p", { class: "empty" }, "Checked against the recording before the replay: ",
      h("a", { class: `badge ${VALIDATION_BADGE[v.status]}`, href: `#/scripts/${state.script}/validation` }, v.label)) : null,
    run.error ? h("div", { class: "warn" }, run.error) : null,
    (s.warnings || []).map(w => h("div", { class: "warn" }, w)),
    h("div", { class: "toolbar" },
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: state.debugFailedOnly, "data-key": "d-failed",
        onchange: (e) => { state.debugFailedOnly = e.target.checked; renderScriptPage("d-failed"); } }), "Failed requests only"),
      h("form", { class: "inline", role: "search", onsubmit: (e) => { e.preventDefault(); searchDebug(e.target.elements.q.value); } },
        h("label", {}, "Search requests and responses", h("input", { type: "search", name: "q", value: search ? search.q : "",
          "data-key": "d-search", placeholder: "text in a URL, header or body" })),
        h("button", { class: "btn quiet small", type: "submit" }, "Search"),
        search ? h("button", { class: "btn quiet small", type: "button", onclick: () => searchDebug("") }, "Clear") : null)),
    search ? h("p", { class: "empty", role: "status" }, search.error || `${search.hits.length} result${search.hits.length === 1 ? "" : "s"} contain "${search.q}"`
      + (search.hits.length ? `: ${[...new Set(search.hits.map(x => `#${x.index}`))].join(", ")}.` : ".")) : null,
    h("div", { class: "tablewrap" }, h("table", { class: "reqs debug" },
      h("thead", {}, h("tr", {}, ["", "Request", "Business function", "Recorded", "Replay", "Time", "What happened"].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, rows.flatMap(i => [
        h("tr", { class: [i.index === s.first_failure ? "first" : "", i.failed_runs || i.state === "not_run" ? "failrow" : ""].join(" ").trim() || null },
          h("td", {}, statusCell(i)),
          h("td", {}, i.state === "ran"
            ? h("button", { class: "linklike", "data-key": `d-row-${i.index}`, "aria-expanded": String(!!(state.debugSample && state.debugSample.index === i.index)),
                onclick: () => (state.debugSample && state.debugSample.index === i.index ? (state.debugSample = null, renderScriptPage(`d-row-${i.index}`)) : openSample(i.index)) },
                `#${i.index} ${i.label}`)
            : `#${i.index} ${i.label}`),
          h("td", {}, i.transaction || ""),
          h("td", {}, i.recorded_status == null ? "–" : String(i.recorded_status)),
          h("td", {}, i.status || "–", i.runs > 1 ? h("small", { class: "src" }, `${i.runs - i.failed_runs} of ${i.runs} runs OK`) : null),
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
    compareSection(run),
    h("div", { class: "section" }, h("h2", {}, "For advanced users"),
      h("div", { class: "links" },
        h("a", { class: "btn quiet", href: `/debug-runs/${run.id}/jmeter.log`, target: "_blank", rel: "noopener" }, "JMeter log"),
        can("tester") ? h("a", { class: "btn quiet", href: `/debug-runs/${run.id}/results.xml` }, "Raw results (.jtl, unmasked)") : null)),
  ];
}
