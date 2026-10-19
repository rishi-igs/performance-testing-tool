"use strict";
// Scripts: import a HAR or .jmx, review and group its requests, then design and replay it.

const SCRIPT_TABS = [
  ["requests", "Requests"], ["correlation", "Correlation"], ["parameters", "Parameters"],
  ["checks", "Checks"], ["settings", "Run-time settings"], ["debug", "Debug replay"],
];

$("#importform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = e.target.elements.file.files[0], btn = $("#importbtn"), err = $("#importerror");
  err.textContent = "";
  if (!file) return;
  const jmx = /\.jmx$/i.test(file.name);
  btn.disabled = true; btn.textContent = "Importing…";
  try {
    const name = file.name.replace(/\.(har|jmx|json|xml)$/i, "").slice(0, 100);
    const created = await api(inProject(`/scripts/import-${jmx ? "jmx" : "har"}?name=${encodeURIComponent(name)}`),
      { method: "POST", body: file, headers: { "Content-Type": jmx ? "application/xml" : "application/json" } });
    e.target.reset();
    state.scriptView = created;
    await refreshScripts();
    go(`#/scripts/${created.id}`);
  } catch (ex) { err.textContent = ex.message; }
  finally { btn.disabled = false; btn.textContent = "Import and review"; }
});

async function refreshScripts() {
  state.scripts = await api(inProject("/scripts"));
  const box = $("#scripts");
  if (!state.scripts.length) { box.replaceChildren(h("p", { class: "empty" }, "No imports yet.")); return; }
  box.replaceChildren(...state.scripts.map(s => h("a", {
      class: "run", href: `#/scripts/${s.id}`, "aria-current": s.id === state.script ? "page" : null },
    h("div", { class: "name" }, s.name),
    h("div", { class: "sub" }, h("span", { class: "badge source" }, s.source.toUpperCase()),
      ` ${fmt(s.request_count)} requests · ${ago(s.created_at)}`))));
}

function renderScriptsHome() {
  state.script = null;
  refreshScripts();
  $("#main").replaceChildren(h("div", { class: "welcome" },
    h("h1", {}, "Build a script from a recording"),
    h("p", {}, "Record the user flow in your browser (DevTools > Network > Save all as HAR) or with JMeter's recorder, then import the file."),
    h("ol", { class: "steps" },
      h("li", {}, "Requests: keep the calls that matter and group them into business functions."),
      h("li", {}, "Correlation: values the server issues (tokens, IDs) are captured from responses. Most are found automatically."),
      h("li", {}, "Parameters: give each virtual user its own test data."),
      h("li", {}, "Checks: define what a correct response looks like."),
      h("li", {}, "Debug replay: run it once and fix what fails, then export it or use it in a test.")),
    h("p", {}, "Use test accounts when you record: request bodies are kept as recorded.")));
}

async function openScript(id, tab = "requests", sub = null) {
  clearTimeout(state.timer);
  if (state.script !== id) {
    state.filters = { q: "", method: "", domain: "", showExcluded: false };
    state.exportOpts = { users: 1, loops: 1, ramp: 0 };
    state.editing = null;
    state.debugRuns = []; state.debugRun = null; state.debugSample = null; state.autoJob = null;
  }
  state.script = id;
  state.scriptTab = SCRIPT_TABS.some(([key]) => key === tab) ? tab : "requests";
  if (!state.scriptView || state.scriptView.id !== id) {
    try { state.scriptView = await api(`/scripts/${id}`); }
    catch (e) { setMain(h("div", { class: "notice" }, e.message)); return; }
  }
  refreshScripts();
  if (state.scriptTab === "debug") { await loadDebug(sub); return; }
  if (state.scriptTab === "correlation") await loadAutoJob();
  renderScriptPage();
}

function setSaveState(text) { const el = $("#savestate"); if (el) el.textContent = text; }
function showError(text) { const el = $("#designerror"); if (el) el.textContent = text; }

async function saveFilter(body) {
  const key = focusedKey();
  setSaveState("Saving…");
  try {
    state.scriptView = await api(`/scripts/${state.script}/filter`, { method: "PUT", body: JSON.stringify(body) });
    renderScriptPage(key);
    setSaveState("Saved");
  } catch (e) { setSaveState(""); showError(`Not saved: ${e.message}`); }
}

// Replace parts of the design (correlations, parameters, ...). Returns true when saved.
async function saveDesign(parts) {
  const key = focusedKey();
  setSaveState("Saving…");
  try {
    state.scriptView = await api(`/scripts/${state.script}/design`, { method: "PUT", body: JSON.stringify(parts) });
    state.editing = null;
    renderScriptPage(key);
    setSaveState("Saved");
    return true;
  } catch (e) { setSaveState(""); showError(`Not saved: ${e.message}`); return false; }
}

async function deleteScript() {
  if (!confirm(`Delete the import "${state.scriptView.name}" with its data files and debug runs? This cannot be undone.`)) return;
  try { await api(`/scripts/${state.script}`, { method: "DELETE" }); }
  catch (e) { showError(e.message); return; }
  state.script = null; state.scriptView = null;
  await refreshScripts();
  go("#/scripts");
}

function exportHref() {
  const o = state.exportOpts;
  return `/scripts/${state.script}/export-jmx?users=${o.users}&loops=${o.loops}&ramp_up_seconds=${o.ramp}`;
}
const exportsZip = (s) => s.files.length > 0 || s.design.parameters.some(p => p.type === "list" && p.selection === "sequential");

function itemLabel(index) {
  const it = state.scriptView.items[index];
  return it ? `#${index} ${it.label}` : `#${index}`;
}
// Requests a rule can point at: kept HTTP requests, in run order.
const keptRequests = () => state.scriptView.items.filter(i => i.include && i.kind === "http");
const requestOptions = (withAll) => [...(withAll ? [["", "Every request"]] : []),
  ...keptRequests().map(i => [i.index, itemLabel(i.index)])];

function renderScriptPage(focusKey) {
  const s = state.scriptView, sum = s.summary, v = s.validation;
  const scrollY = window.scrollY;
  clearCharts();
  const counts = {
    requests: sum.kept, correlation: s.design.correlations.length,
    parameters: s.design.parameters.length + s.design.data_files.length, checks: s.design.checks.length,
  };
  const missing = [...Object.keys(v.unresolved || {}), ...(v.empty || [])];
  const tabs = { requests: requestsTab, correlation: correlationTab, parameters: parametersTab,
    checks: checksTab, settings: settingsTab, debug: debugTab };
  const label = SCRIPT_TABS.find(([key]) => key === state.scriptTab)[1];

  setMain(
    h("div", { class: "head" },
      h("div", {},
        h("h1", {}, s.name, " ", h("span", { class: "badge source" }, s.source.toUpperCase())),
        h("p", { class: "verdict" }, `${fmt(sum.captured)} captured, ${fmt(sum.kept)} kept, ${fmt(sum.excluded)} excluded · `
          + `${sum.transactions} business function${sum.transactions === 1 ? "" : "s"}`)),
      h("div", { class: "actions" },
        h("button", { class: "btn", onclick: startDebug, disabled: !sum.kept }, "Debug replay"),
        sum.kept ? h("a", { class: "btn quiet", id: "exportlink", href: exportHref() }, exportsZip(s) ? "Download .zip" : "Download .jmx") : null,
        h("button", { class: "btn quiet", onclick: deleteScript }, "Delete"))),
    missing.length ? h("div", { class: "warn critical" },
      `${missing.length} variable${missing.length === 1 ? " has" : "s have"} no value yet: ${missing.map(varRef).join(", ")}. `,
      h("a", { href: `#/scripts/${s.id}/correlation` }, "Correlate"), " or ",
      h("a", { href: `#/scripts/${s.id}/parameters` }, "define them as parameters"), ".") : null,
    (v.warnings || []).map(w => h("div", { class: "warn" }, w)),
    h("nav", { class: "tabs", "aria-label": "Script sections" }, SCRIPT_TABS.map(([key, text]) =>
      h("a", { class: "tab", href: `#/scripts/${s.id}/${key}`, "aria-current": state.scriptTab === key ? "page" : null },
        text, counts[key] != null ? h("span", { class: "count" }, fmt(counts[key])) : null))),
    h("div", { class: "statusline" },
      h("div", { class: "formerror", id: "designerror", role: "alert" }),
      h("span", { class: "savestate", id: "savestate", role: "status" })),
    h("section", { class: "tabpanel", "aria-label": label }, tabs[state.scriptTab]()));
  window.scrollTo(0, scrollY);
  restoreFocus(focusKey);
}

// ---- Requests tab: filter, group into business functions, export ------------------------

function parseRules(text) {
  return text.split("\n").map(l => l.trim()).filter(Boolean).map((line, i) => {
    const at = line.indexOf("=");
    if (at < 1 || !line.slice(at + 1).trim()) throw new Error(`Rule line ${i + 1} must look like Name = URL pattern`);
    return { name: line.slice(0, at).trim(), url_pattern: line.slice(at + 1).trim() };
  });
}

function scriptRow(it) {
  const why = !it.include ? (it.include_source === "manual" ? "Excluded by you" : it.auto_reason)
    : (it.include_source === "manual" && it.auto_reason ? `Kept by you (rule: ${it.auto_reason})` : null);
  const where = it.url ? urlParts(it.url) : null;
  return h("tr", { class: it.include ? null : "excluded" },
    h("td", {}, h("input", { type: "checkbox", checked: it.include, "data-key": `i-${it.index}`,
      "aria-label": `Keep ${it.label}`, onchange: (e) => saveFilter({ changes: [{ index: it.index, include: e.target.checked }] }) })),
    h("td", { class: "mono" }, it.method || "–"),
    h("td", { class: "urlcell" }, where ? [h("span", { class: "path" }, where.path), h("span", { class: "hostname" }, `#${it.index} · ${where.host}`)] : it.label),
    h("td", {}, it.status == null ? "–" : String(it.status)),
    h("td", {}, it.resource_type || (it.mime ? it.mime.split("/").pop() : it.kind === "other" ? "JMeter" : "–")),
    h("td", { class: "num" }, it.size == null ? "–" : fmtBytes(it.size)),
    h("td", { class: "num" }, it.time_ms == null ? "–" : `${fmt(it.time_ms)} ms`),
    h("td", {}, h("input", { class: "fname", value: it.transaction, list: "fnames", maxlength: 80, "data-key": `t-${it.index}`,
        "aria-label": `Business function for ${it.label}`,
        onchange: (e) => saveFilter({ changes: [{ index: it.index, transaction: e.target.value.trim() || null }] }) }),
      it.transaction_source === "auto" ? null : h("small", { class: "src" }, it.transaction_source === "rule" ? "from a rule" : "edited")),
    h("td", { class: "reason" }, why || it.note || ""));
}

function groupRow(chunk) {
  const first = chunk[0], kept = chunk.filter(i => i.include).length;
  return h("tr", { class: "grouprow" }, h("th", { colspan: 9, scope: "colgroup" },
    h("div", { class: "groupline" },
      h("span", { class: "gnum" }, first.transaction_number ? pad2(first.transaction_number) : "–"),
      h("input", { class: "gname", value: first.transaction, maxlength: 80, "data-key": `g-${first.index}`,
        "aria-label": `Rename business function ${first.transaction}`,
        onchange: (e) => { const v = e.target.value.trim();
          if (v) saveFilter({ changes: chunk.map(i => ({ index: i.index, transaction: v })) }); } }),
      h("span", { class: "gcount" }, `${kept} kept${chunk.length > kept ? `, ${chunk.length - kept} excluded` : ""}`))));
}

function requestsTab() {
  const s = state.scriptView, f = state.filters, sum = s.summary;
  const q = f.q.toLowerCase();
  const visible = s.items.filter(i => (f.showExcluded || i.include)
    && (!f.method || i.method === f.method) && (!f.domain || i.host === f.domain)
    && (!q || `${i.label} ${i.url || ""} ${i.transaction}`.toLowerCase().includes(q)));
  const chunks = [];
  for (const it of visible) {
    const last = chunks[chunks.length - 1];
    if (last && last[0].transaction === it.transaction) last.push(it); else chunks.push([it]);
  }
  const options = (values, current) => [h("option", { value: "" }, "All"),
    ...values.map(v => h("option", { value: v, selected: v === current }, v))];
  const hosts = [...new Set(s.items.map(i => i.host).filter(Boolean))].sort();
  const methods = [...new Set(s.items.map(i => i.method).filter(Boolean))].sort();
  const o = state.exportOpts;
  const exportInput = (label, key, max) => h("label", {}, label, h("input", { type: "number", min: key === "ramp" ? 0 : 1, max,
    value: o[key], oninput: (e) => { o[key] = Math.max(+e.target.min, parseInt(e.target.value, 10) || 0);
      const a = $("#exportlink"); if (a) a.href = exportHref(); } }));

  return [
    s.transactions.length ? h("ol", { class: "flow", "aria-label": "Business functions in run order" },
      s.transactions.map(t => h("li", {}, `${pad2(t.number)} ${t.name} `, h("small", {}, `(${t.requests})`)))) : null,
    s.warnings.length ? h("div", { class: "section" }, s.warnings.map(w => h("div", { class: "warn" }, w))) : null,

    s.source === "har" ? h("div", { class: "section" }, h("h2", {}, "Export load"),
      h("div", { class: "exportbox" },
        exportInput("Users", "users"), exportInput("Loops", "loops", 100000), exportInput("Ramp-up (s)", "ramp", 3600),
        h("p", { class: "empty" }, "Used by Download. Imported .jmx files keep their own thread groups."))) : null,

    h("div", { class: "toolbar" },
      h("label", {}, "Search", h("input", { type: "search", value: f.q, "data-key": "f-q", placeholder: "URL or function",
        oninput: (e) => { f.q = e.target.value; renderScriptPage("f-q"); } })),
      h("label", {}, "Method", h("select", { "data-key": "f-m", onchange: (e) => { f.method = e.target.value; renderScriptPage("f-m"); } }, options(methods, f.method))),
      h("label", {}, "Domain", h("select", { "data-key": "f-d", onchange: (e) => { f.domain = e.target.value; renderScriptPage("f-d"); } }, options(hosts, f.domain))),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: f.showExcluded, "data-key": "f-x",
        onchange: (e) => { f.showExcluded = e.target.checked; renderScriptPage("f-x"); } }), `Show excluded (${fmt(sum.excluded)})`)),

    h("div", { class: "bulk" },
      h("span", {}, `${fmt(visible.length)} shown:`),
      h("button", { class: "btn quiet small", onclick: () => saveFilter({ changes: visible.map(i => ({ index: i.index, include: true })) }) }, "Keep all shown"),
      h("button", { class: "btn quiet small", onclick: () => saveFilter({ changes: visible.map(i => ({ index: i.index, include: false })) }) }, "Exclude all shown"),
      h("button", { class: "btn quiet small", onclick: () => saveFilter({ changes: visible.map(i => ({ index: i.index, include: null, transaction: null })) }) }, "Reset shown to automatic"),
      h("button", { class: "btn quiet small", "aria-pressed": String(s.rules.keep_only_api),
        onclick: () => saveFilter({ rules: { ...s.rules, keep_only_api: !s.rules.keep_only_api } }) }, "Keep API calls only")),

    h("div", { class: "tablewrap" }, h("table", { class: "reqs" },
      h("thead", {}, h("tr", {}, ["Keep", "Method", "Request", "Status", "Type", "Size", "Time", "Business function", "Why"]
        .map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, visible.length ? chunks.flatMap(c => [groupRow(c), ...c.map(scriptRow)])
        : h("tr", {}, h("td", { colspan: 9, class: "empty" }, "No requests match these filters."))))),
    h("datalist", { id: "fnames" }, [...new Set(s.items.map(i => i.transaction))].map(n => h("option", { value: n }))),

    h("details", { class: "rules section", open: state.rulesOpen, ontoggle: (e) => { state.rulesOpen = e.target.open; } },
      h("summary", {}, "Filter and grouping rules"),
      h("form", { onsubmit: (e) => { e.preventDefault(); const el = e.target.elements;
          let rules;
          try { rules = parseRules(el.tx.value); } catch (ex) { $("#ruleserror").textContent = ex.message; return; }
          saveFilter({ rules: { ...s.rules, transaction_rules: rules, idle_gap_seconds: +el.gap.value,
            exclude_extensions: listOf(el.ext.value), exclude_mime_types: listOf(el.mime.value),
            exclude_domains: listOf(el.domains.value), exclude_methods: listOf(el.methods.value) } }); } },
        h("label", {}, "Business-function rules, one per line: Name = URL pattern",
          h("textarea", { name: "tx", rows: 4, placeholder: "Login = */auth/*\nSearch = */api/search*" },
            s.rules.transaction_rules.map(r => `${r.name} = ${r.url_pattern}`).join("\n"))),
        h("small", {}, "* matches anything. The first matching rule wins over automatic grouping; your own edits win over rules."),
        h("label", {}, "Start a new business function after this many idle seconds (0 turns it off)",
          h("input", { name: "gap", type: "number", min: 0, max: 600, step: 0.5, value: s.rules.idle_gap_seconds })),
        h("label", {}, "Exclude file extensions", h("textarea", { name: "ext", rows: 2 }, s.rules.exclude_extensions.join(", "))),
        h("label", {}, "Exclude response content types", h("textarea", { name: "mime", rows: 2 }, s.rules.exclude_mime_types.join(", "))),
        h("label", {}, "Exclude domains (analytics, ads, chat widgets)", h("textarea", { name: "domains", rows: 5 }, s.rules.exclude_domains.join("\n"))),
        h("label", {}, "Exclude methods", h("input", { name: "methods", value: s.rules.exclude_methods.join(", ") })),
        h("button", { class: "btn", type: "submit" }, "Apply rules"),
        h("div", { class: "formerror", id: "ruleserror", role: "alert" }))),
  ];
}
