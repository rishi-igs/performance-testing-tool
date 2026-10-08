"use strict";
// Script design tabs: Correlation, Parameters, Checks, Run-time settings (VuGen's equivalents).

const EXTRACTORS = [["json", "JSON path"], ["boundary", "Left and right boundaries"], ["regex", "Regular expression"]];
const PARAM_TYPES = [["list", "List of values"], ["random_number", "Random number"], ["unique_number", "Unique number"],
  ["uuid", "UUID"], ["date", "Date or time"], ["random_string", "Random text"]];
const CHECK_TYPES = [["text", "Response contains text"], ["not_text", "Response does not contain text"],
  ["regex", "Response matches a regular expression"], ["json_path", "JSON field exists or equals"],
  ["header", "A response header contains text"], ["duration", "Response time at most (ms)"],
  ["size_max", "Response body at most (bytes)"]];
const DATE_FORMATS = ["yyyy-MM-dd", "dd/MM/yyyy", "MM/dd/yyyy", "yyyy-MM-dd'T'HH:mm:ss", "epoch_ms", "epoch_s"];

// Form controls are read through form.elements, so none may be named after one of its own
// properties (item, length, namedItem): those names would return the property instead.
const editingKind = (kind) => (state.editing && state.editing.kind === kind ? state.editing : null);
function startEditing(kind, index = null, draft = {}) { state.editing = { kind, index, draft }; renderScriptPage(`${kind[0]}-first`); }
function stopEditing() { state.editing = null; renderScriptPage(); }
// Open an editor on another tab of the same script.
function editIn(tab, kind, draft) {
  state.editing = { kind, index: null, draft };
  if (state.scriptTab === tab) renderScriptPage(`${kind[0]}-first`);
  else location.hash = `#/scripts/${state.script}/${tab}`;
}
const intOr = (value, fallback) => (value === "" || value == null || Number.isNaN(+value) ? fallback : parseInt(value, 10));

function knownVariables() {
  const d = state.scriptView.design;
  return [...new Set([...d.correlations.map(c => c.variable), ...d.parameters.map(p => p.name),
    ...d.data_files.flatMap(f => f.columns), ...state.scriptView.variables])].sort();
}

function problemsPanel() {
  const v = state.scriptView.validation;
  const problems = [
    ...Object.entries(v.unresolved || {}).map(([name, used]) =>
      [name, `is sent by ${used.length ? used.map(i => `#${i}`).join(", ") : "shared headers"} but nothing sets it.`]),
    ...(v.empty || []).map(name => [name, "is a recorded credential that has no value yet."]),
  ];
  if (!problems.length) return null;
  return h("div", { class: "section" }, h("h2", {}, "Variables without a value"),
    v.scripted ? h("p", { class: "empty" }, "This plan has JSR223 or BeanShell elements, which may set some of these.") : null,
    h("ul", { class: "problems" }, problems.map(([name, text]) => h("li", {},
      h("code", {}, varRef(name)), ` ${text} `,
      h("button", { class: "btn quiet small", onclick: () => editIn("correlation", "correlation", { variable: name }) }, "Correlate"),
      " ",
      h("button", { class: "btn quiet small", onclick: () => editIn("parameters", "parameter", { name, type: "list" }) }, "Make it a parameter")))));
}

// ---- Correlation --------------------------------------------------------------------------

function extractorSummary(c) {
  if (c.extractor === "json") return ["JSON path ", h("code", {}, c.expression)];
  if (c.extractor === "boundary") return [`Between${c.scope === "headers" ? " (headers)" : ""} `, h("code", {}, c.expression), " and ", h("code", {}, c.right)];
  return [`Regex${c.scope === "headers" ? " on headers" : ""} `, h("code", {}, c.expression)];
}

function correlationEditor(editing) {
  const rules = state.scriptView.design.correlations;
  const existing = editing.index != null ? rules[editing.index] : null;
  const first = keptRequests()[0];
  const c = existing || { variable: "", source: first ? first.index : 0, extractor: "json", expression: "$.", right: "",
    scope: "body", match: 1, replace: "", origin: "manual", ...editing.draft };
  return h("form", { class: "editor", onsubmit: (e) => {
      e.preventDefault();
      const el = e.target.elements;
      const extractor = el.extractor.value;
      const rule = { ...(existing || { origin: "manual" }), variable: el.variable.value.trim(), source: +el.source.value,
        extractor, expression: el.expression.value, right: extractor === "boundary" ? el.right.value : null,
        scope: extractor === "json" ? "body" : el.scope.value, match: intOr(el.match.value, 1),
        replace: el.replace.value || null };
      saveDesign({ correlations: existing ? rules.map((r, i) => (i === editing.index ? rule : r)) : [...rules, rule] });
    } },
    h("h3", {}, existing ? `Edit ${varRef(c.variable)}` : "Add a correlation"),
    h("div", { class: "row" },
      field("Variable name", h("input", { name: "variable", required: true, pattern: "[A-Za-z_][A-Za-z0-9_]*", maxlength: 64,
        value: c.variable, "data-key": "c-first" })),
      field("Capture it from the response of", choice("source", requestOptions(false), c.source))),
    h("div", { class: "row" },
      field("How to find it", choice("extractor", EXTRACTORS, c.extractor)),
      field("Look in", choice("scope", [["body", "Response body"], ["headers", "Response headers"]], c.scope),
        "JSON paths always read the body")),
    field("Expression", h("input", { name: "expression", required: true, value: c.expression }),
      "JSON path: $.token or $.items[0].id · Boundaries: the text right before the value · Regular expression: one (group), e.g. token=(\\w+)"),
    h("div", { class: "row" },
      field("Right boundary", h("input", { name: "right", value: c.right || "" }), "Boundaries only: the text right after the value"),
      field("Which match", h("input", { name: "match", type: "number", min: 0, max: 1000, value: c.match }), "1 = first, 2 = second, 0 = a random one")),
    field("Recorded value to replace (optional)", h("input", { name: "replace", value: c.replace || "" }),
      "Every whole occurrence in later requests becomes the variable. Leave empty when they already send it."),
    h("div", { class: "actions" },
      h("button", { class: "btn", type: "submit" }, "Save correlation"),
      h("button", { class: "btn quiet", type: "button", onclick: stopEditing }, "Cancel")));
}

// ---- Correlate automatically: replay, find the values in what the server sent, add the rules ----

async function loadAutoJob() {
  const script = state.script;
  let job;
  try { job = await api(`/scripts/${script}/autocorrelate`); } catch (_) { return; }
  if (state.script !== script) return;
  const finished = state.autoJob && state.autoJob.status === "running" && job.status !== "running";
  state.autoJob = job;
  if (finished) { try { state.scriptView = await api(`/scripts/${script}`); } catch (_) {} }
  if (job.status === "running") { clearTimeout(state.timer); state.timer = setTimeout(pollAutoJob, 1500); }
}

async function pollAutoJob() {
  const script = state.script;
  await loadAutoJob();
  if (state.scriptTab === "correlation" && state.script === script) renderScriptPage(focusedKey());
}

async function startAutoCorrelate() {
  showError("");
  try { state.autoJob = await api(`/scripts/${state.script}/autocorrelate`, { method: "POST" }); }
  catch (e) { showError(`Not started: ${e.message}`); return; }
  renderScriptPage();
  clearTimeout(state.timer);
  state.timer = setTimeout(pollAutoJob, 1500);
}

function autoJobSummary(job) {
  if (job.status === "failed") return job.message;
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const found = job.correlations.length
    ? `Added ${plural(job.correlations.length, "correlation")} (${job.correlations.map(varRef).join(", ")})`
    : "No new correlations were needed";
  const generated = job.parameters.length
    ? ` and ${plural(job.parameters.length, "generated value")} (${job.parameters.map(varRef).join(", ")})` : "";
  return `${found}${generated} in ${plural(job.replays.length, "replay")}. ${job.message}`;
}

function autoCorrelatePanel() {
  const s = state.scriptView;
  const job = state.autoJob && state.autoJob.status !== "idle" ? state.autoJob : null;
  const running = !!job && job.status === "running";
  const last = job && job.replays.length ? job.replays[job.replays.length - 1] : null;
  return h("div", { class: "section" }, h("h2", {}, "Correlate automatically"),
    h("p", { class: "intro" }, s.source === "jmx"
      ? "A .jmx file holds the requests but not what the server answered. This replays the script with one user, matches each recorded value with the fresh one the server sends (by field name or by value), adds the rules, and replays again until nothing new turns up (at most 4 replays)."
      : "Rules were found in the recorded responses when the HAR was imported. Replaying finds any the recording missed, for example when it was saved without response bodies."),
    running ? h("p", { class: "notice", role: "status" }, `${job.message}… (replay ${job.round} of at most ${job.max_replays})`) : null,
    job && !running ? h("div", { class: job.status === "failed" ? "warn critical" : "notice good", role: "status" },
      autoJobSummary(job), last ? [" ", h("a", { href: `#/scripts/${s.id}/debug/${last}` }, "Open the last replay")] : null) : null,
    can("tester") ? h("button", { class: "btn", type: "button", onclick: startAutoCorrelate, disabled: running || !s.summary.kept },
      running ? "Correlating…" : "Correlate automatically") : null);
}

function correlationTab() {
  const s = state.scriptView, rules = s.design.correlations, used = s.validation.used || {};
  const editing = editingKind("correlation");
  return [
    h("p", { class: "intro" }, "Values the server issues during a session (login tokens, CSRF tokens, new IDs) must be captured from a response and sent back, or the replay is rejected. Rules found automatically are marked."),
    autoCorrelatePanel(),
    rules.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["Variable", "Captured from", "How", "Sent by", "", ""].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, rules.map((c, n) => h("tr", {},
        h("td", { class: "mono" }, varRef(c.variable)),
        h("td", {}, itemLabel(c.source)),
        h("td", { class: "how" }, extractorSummary(c), c.replace ? h("small", { class: "src" }, `replaces the recorded value in later requests`) : null),
        h("td", {}, (used[c.variable] || []).length ? used[c.variable].map(i => `#${i}`).join(", ") : "nothing yet"),
        h("td", {}, h("span", { class: `badge ${c.origin === "auto" ? "source" : "stopped"}` }, c.origin === "auto" ? "found automatically" : "added by you")),
        h("td", { class: "rowactions" },
          h("button", { class: "btn quiet small", onclick: () => startEditing("correlation", n) }, "Edit"),
          h("button", { class: "btn quiet small", "aria-label": `Delete correlation ${c.variable}`,
            onclick: () => saveDesign({ correlations: rules.filter((_, i) => i !== n) }) }, "Delete")))))))
      : h("p", { class: "empty" }, "No correlations yet."),
    editing ? correlationEditor(editing)
      : h("button", { class: "btn quiet", onclick: () => startEditing("correlation") }, "Add a correlation"),
    problemsPanel(),
  ];
}

// ---- Parameters ---------------------------------------------------------------------------

function paramSummary(p) {
  switch (p.type) {
    case "list": return `${p.values.length} value${p.values.length === 1 ? "" : "s"}, ${p.selection === "random" ? "random pick" : "next value"} each iteration`;
    case "random_number": return `${p.minimum} to ${p.maximum}, new ${p.update === "occurrence" ? "at each use" : "each iteration"}`;
    case "unique_number": return `from ${p.start}, step ${p.increment}, ${p.per_user ? "per user" : "shared by all users"}${p.format ? `, format ${p.format}` : ""}`;
    case "uuid": return "a new UUID at each use";
    case "date":
      if (p.format === "epoch_ms") return "Unix time in milliseconds";
      if (p.format === "epoch_s") return "Unix time in seconds";
      return `${p.format}, today${p.offset_days ? ` ${p.offset_days > 0 ? "+" : ""}${p.offset_days} days` : ""}`;
    default: return `${p.length} random letters and digits`;
  }
}

function readParamForm(form, base) {
  const el = form.elements;
  const value = (name, fallback) => (el[name] ? el[name].value : fallback);
  return {
    ...base, name: el.name.value.trim(), type: el.type.value,
    values: el.list_values ? el.list_values.value.split("\n").map(v => v.trim()).filter(Boolean) : base.values,
    selection: value("selection", base.selection), minimum: intOr(value("minimum"), base.minimum),
    maximum: intOr(value("maximum"), base.maximum), update: value("update", base.update),
    start: intOr(value("start"), base.start), increment: intOr(value("increment"), base.increment),
    per_user: el.per_user ? el.per_user.checked : base.per_user, format: value("format", base.format),
    offset_days: intOr(value("offset_days"), base.offset_days), length: intOr(value("str_length"), base.length),
  };
}

function parameterEditor(editing) {
  const params = state.scriptView.design.parameters;
  const existing = editing.index != null ? params[editing.index] : null;
  const p = { name: "", type: "list", values: [], selection: "sequential", minimum: 1, maximum: 1000, update: "iteration",
    start: 1, increment: 1, per_user: false, format: "", offset_days: 0, length: 10, ...(existing || {}), ...editing.draft };
  const typed = {
    list: [field("Values, one per line", h("textarea", { name: "list_values", rows: 5 }, p.values.join("\n"))),
      field("Each iteration takes", choice("selection", [["sequential", "The next value (CSV file)"], ["random", "A random value"]], p.selection))],
    random_number: [h("div", { class: "row" }, field("From", h("input", { name: "minimum", type: "number", value: p.minimum })),
        field("To", h("input", { name: "maximum", type: "number", value: p.maximum }))),
      field("New value", choice("update", [["iteration", "Each iteration"], ["occurrence", "At each use"]], p.update))],
    unique_number: [h("div", { class: "row" }, field("Start at", h("input", { name: "start", type: "number", value: p.start })),
        field("Step", h("input", { name: "increment", type: "number", value: p.increment }))),
      h("label", { class: "check" }, h("input", { type: "checkbox", name: "per_user", checked: p.per_user }), "Count separately for each user"),
      field("Format (optional)", h("input", { name: "format", value: p.format, placeholder: "000000" }), "Java number format; 000000 pads to six digits")],
    uuid: [h("p", { class: "empty" }, "Each use sends a new random UUID.")],
    date: [h("div", { class: "row" },
        field("Format", h("input", { name: "format", value: p.format || "yyyy-MM-dd", list: "dateformats" }), "Java date format, or epoch_ms / epoch_s for Unix time"),
        field("Days from today", h("input", { name: "offset_days", type: "number", value: p.offset_days }))),
      h("datalist", { id: "dateformats" }, DATE_FORMATS.map(f => h("option", { value: f })))],
    random_string: [field("Length", h("input", { name: "str_length", type: "number", min: 1, max: 1000, value: p.length }))],
  };
  return h("form", { class: "editor", onsubmit: (e) => {
      e.preventDefault();
      const next = readParamForm(e.target, p);
      saveDesign({ parameters: existing ? params.map((x, i) => (i === editing.index ? next : x)) : [...params, next] });
    } },
    h("h3", {}, existing ? `Edit ${varRef(p.name)}` : "Add a parameter"),
    h("div", { class: "row" },
      field("Variable name", h("input", { name: "name", required: true, pattern: "[A-Za-z_][A-Za-z0-9_]*", maxlength: 64,
        value: p.name, "data-key": "p-first" })),
      field("Type", choice("type", PARAM_TYPES, p.type, { "data-key": "p-type", onchange: (e) => {
        state.editing.draft = { ...readParamForm(e.target.form, p), type: e.target.value };
        renderScriptPage("p-type"); } }))),
    typed[p.type],
    h("div", { class: "actions" },
      h("button", { class: "btn", type: "submit" }, "Save parameter"),
      h("button", { class: "btn quiet", type: "button", onclick: stopEditing }, "Cancel")));
}

async function uploadDataFile(form) {
  const file = form.elements.file.files[0];
  if (!file) return;
  let name = file.name.replace(/[^A-Za-z0-9_.-]/g, "_").replace(/^[^A-Za-z0-9_]+/, "");
  if (!/\.(csv|txt)$/i.test(name)) name += ".csv";
  setSaveState("Uploading…");
  try {
    state.scriptView = await api(`/scripts/${state.script}/files?name=${encodeURIComponent(name.toLowerCase())}&header=${form.elements.header.checked}`,
      { method: "POST", body: file, headers: { "Content-Type": "text/csv" } });
    renderScriptPage();
    setSaveState("Uploaded");
  } catch (e) { setSaveState(""); showError(`Not uploaded: ${e.message}`); }
}

function dataFilesSection() {
  const s = state.scriptView, files = s.design.data_files;
  const update = (n, patch) => saveDesign({ data_files: files.map((f, i) => (i === n ? { ...f, ...patch } : f)) });
  const atEnd = (f) => (f.stop_at_end ? "stop" : f.recycle ? "recycle" : "eof");
  return h("div", { class: "section" }, h("h2", {}, "Data files"),
    h("p", { class: "intro" }, "A CSV file of test data, such as user names and passwords. Each column becomes a variable; each iteration reads the next row."),
    files.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["File", "Variables", "Rows", "Rows are", "At the end of the file", ""].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, files.map((f, n) => h("tr", {},
        h("td", { class: "mono" }, f.name),
        h("td", { class: "mono" }, f.columns.map(varRef).join(", ")),
        h("td", { class: "num" }, fmt(f.rows)),
        h("td", {}, choice("sharing", [["all", "Shared: each row used once"], ["group", "Read from the top per thread group"],
          ["user", "Read from the top by each user"]], f.sharing, { "aria-label": `How users share ${f.name}`,
          onchange: (e) => update(n, { sharing: e.target.value }) })),
        h("td", {}, choice("end", [["recycle", "Start again at the top"], ["stop", "Stop that user"], ["eof", "Continue with <EOF>"]],
          atEnd(f), { "aria-label": `End of ${f.name}`,
          onchange: (e) => update(n, { recycle: e.target.value === "recycle", stop_at_end: e.target.value === "stop" }) })),
        h("td", { class: "rowactions" }, h("button", { class: "btn quiet small", "aria-label": `Delete ${f.name}`,
          onclick: async () => { try { state.scriptView = await api(`/scripts/${state.script}/files/${encodeURIComponent(f.name)}`, { method: "DELETE" }); renderScriptPage(); } catch (e) { showError(e.message); } } }, "Delete")))))))
      : null,
    h("form", { class: "inline", onsubmit: (e) => { e.preventDefault(); uploadDataFile(e.target); } },
      field("CSV file", h("input", { type: "file", name: "file", accept: ".csv,.txt,text/csv", required: true })),
      h("label", { class: "check" }, h("input", { type: "checkbox", name: "header", checked: true }), "First line holds the column names"),
      h("button", { class: "btn quiet", type: "submit" }, "Upload")));
}

function replacementsSection() {
  const s = state.scriptView, list = s.design.replacements;
  return h("div", { class: "section" }, h("h2", {}, "Replace recorded text with a variable"),
    h("p", { class: "intro" }, "Every whole occurrence of the text in URLs, bodies and headers becomes the variable, for example the recorded user name becoming ${username}."),
    list.length ? h("ul", { class: "problems" }, list.map((r, n) => h("li", {},
      h("code", {}, /pass|pwd|secret/i.test(r.variable) ? "•••••••• (recorded password)" : r.find), " → ", h("code", {}, varRef(r.variable)), " ",
      h("button", { class: "btn quiet small", "aria-label": `Remove replacement of ${r.find}`,
        onclick: () => saveDesign({ replacements: list.filter((_, i) => i !== n) }) }, "Remove")))) : null,
    h("form", { class: "inline", onsubmit: (e) => { e.preventDefault(); const el = e.target.elements;
        saveDesign({ replacements: [...list, { find: el.find.value, variable: el.variable.value.trim() }] }); } },
      field("Recorded text", h("input", { name: "find", required: true, "data-key": "r-find" })),
      field("Variable", h("input", { name: "variable", required: true, pattern: "[A-Za-z_][A-Za-z0-9_]*", list: "knownvars" })),
      h("datalist", { id: "knownvars" }, knownVariables().map(v => h("option", { value: v }))),
      h("button", { class: "btn quiet", type: "submit" }, "Replace")));
}

function suggestionsSection() {
  const s = state.scriptView, d = s.design;
  const taken = new Set(knownVariables().map(v => v.toLowerCase()));
  const values = (s.suggestions.parameters || []).filter(v => !taken.has(v.name.toLowerCase()));
  if (!values.length) return null;
  const kinds = { uuid: "looks like a UUID the browser generated", timestamp: "looks like the time the browser sent it",
    value: "has no source in any response", search: "is a search term or other input the user typed" };
  const labels = { uuid: "Make it a UUID parameter", timestamp: "Make it the current time", search: "Use a list of test values" };
  return h("div", { class: "section" }, h("h2", {}, "Suggested parameters"),
    h("ul", { class: "problems" }, values.map(v => h("li", {},
      h("code", {}, v.name), ` in ${itemLabel(v.first_used)} ${kinds[v.kind]} (${v.preview}). `,
      v.value ? h("button", { class: "btn quiet small", onclick: () => {
        const name = v.name.replace(/[^A-Za-z0-9_]/g, "_").replace(/^(\d)/, "v_$1");
        const param = v.kind === "uuid" ? { name, type: "uuid" }
          : v.kind === "search" ? { name, type: "list", values: [v.value] }
          : { name, type: "date", format: v.value.length === 13 ? "epoch_ms" : "epoch_s" };
        saveDesign({ parameters: [...d.parameters, param], replacements: [...d.replacements, { find: v.value, variable: name }] });
      } }, labels[v.kind]) : "Replace it with a parameter if the server rejects the recorded value."))));
}

function autoNotes() {
  const notes = ((state.scriptView.suggestions || {}).auto || {}).notes || [];
  return notes.length ? h("div", { class: "notice good" }, h("strong", {}, "Set up automatically. "), notes.join(" ")) : null;
}

function parametersTab() {
  const params = state.scriptView.design.parameters;
  const editing = editingKind("parameter");
  return [
    autoNotes(),
    dataFilesSection(),
    h("div", { class: "section" }, h("h2", {}, "Generated values"),
      params.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["Variable", "Type", "Value", ""].map(t => h("th", { scope: "col" }, t)))),
        h("tbody", {}, params.map((p, n) => h("tr", {},
          h("td", { class: "mono" }, varRef(p.name)),
          h("td", {}, PARAM_TYPES.find(([k]) => k === p.type)[1]),
          h("td", {}, paramSummary(p)),
          h("td", { class: "rowactions" },
            h("button", { class: "btn quiet small", onclick: () => startEditing("parameter", n) }, "Edit"),
            h("button", { class: "btn quiet small", "aria-label": `Delete parameter ${p.name}`,
              onclick: () => saveDesign({ parameters: params.filter((_, i) => i !== n) }) }, "Delete")))))))
        : h("p", { class: "empty" }, "No generated values yet."),
      editing ? parameterEditor(editing) : h("button", { class: "btn quiet", onclick: () => startEditing("parameter") }, "Add a parameter")),
    replacementsSection(),
    suggestionsSection(),
    problemsPanel(),
  ];
}

// ---- Checks -------------------------------------------------------------------------------

function checkSummary(c) {
  switch (c.type) {
    case "json_path": return c.expected == null ? `${c.path} exists` : `${c.path} equals "${c.expected}"`;
    case "duration": return `${fmt(c.limit)} ms or faster`;
    case "size_max": return `${fmt(c.limit)} bytes or smaller`;
    default: return `"${c.value}"`;
  }
}

function checkEditor(editing) {
  const checks = state.scriptView.design.checks;
  const c = { item: "", type: "text", value: "", path: "$.", expected: "", limit: 1000, ...editing.draft };
  const read = (form) => {
    const el = form.elements, type = el.type.value;
    return { item: el.target.value === "" ? null : +el.target.value, type,
      value: el.value ? el.value.value : null, path: el.path ? el.path.value : null,
      expected: el.expected ? (el.expected.value === "" ? null : el.expected.value) : null,
      limit: el.limit ? intOr(el.limit.value, null) : null };
  };
  const typed = {
    json_path: [field("JSON path", h("input", { name: "path", required: true, value: c.path })),
      field("Equals (optional)", h("input", { name: "expected", value: c.expected || "" }), "Leave empty to only check that the field exists")],
    duration: [field("Milliseconds", h("input", { name: "limit", type: "number", min: 1, required: true, value: c.limit }))],
    size_max: [field("Bytes", h("input", { name: "limit", type: "number", min: 1, required: true, value: c.limit }))],
  }[c.type] || [field(c.type === "regex" ? "Regular expression" : "Text", h("input", { name: "value", required: true, value: c.value || "" }))];
  return h("form", { class: "editor", onsubmit: (e) => { e.preventDefault(); saveDesign({ checks: [...checks, read(e.target)] }); } },
    h("h3", {}, "Add a check"),
    h("div", { class: "row" },
      field("On", choice("target", requestOptions(true), c.item ?? "", { "data-key": "k-first" })),
      field("Check", choice("type", CHECK_TYPES, c.type, { "data-key": "k-type", onchange: (e) => {
        state.editing.draft = { ...read(e.target.form), type: e.target.value }; renderScriptPage("k-type"); } }))),
    typed,
    h("div", { class: "actions" },
      h("button", { class: "btn", type: "submit" }, "Add check"),
      h("button", { class: "btn quiet", type: "button", onclick: stopEditing }, "Cancel")));
}

function checksTab() {
  const s = state.scriptView, checks = s.design.checks, v = s.validation;
  const editing = editingKind("check");
  const kept = new Set(keptRequests().map(i => i.index));
  const sameAs = (a, b) => a.item === b.item && a.type === b.type && a.value == b.value && a.path == b.path;
  const suggestions = (s.suggestions.checks || []).filter(x => kept.has(x.item) && !checks.some(c => sameAs(c, { value: null, path: null, ...x })));
  return [
    h("p", { class: "intro" }, "A request only fails on its status code unless you say what a correct response looks like: a 200 can still be an error page or a login screen."),
    (v.status_only_checks || []).length ? h("div", { class: "warn" },
      `${v.status_only_checks.length} kept request${v.status_only_checks.length === 1 ? "" : "s"} only check the status code.`) : null,
    checks.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["On", "Check", "Expect", ""].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, checks.map((c, n) => h("tr", {},
        h("td", {}, c.item == null ? "Every request" : itemLabel(c.item)),
        h("td", {}, CHECK_TYPES.find(([k]) => k === c.type)[1]),
        h("td", { class: "mono" }, checkSummary(c)),
        h("td", { class: "rowactions" }, h("button", { class: "btn quiet small", "aria-label": `Delete check ${n + 1}`,
          onclick: () => saveDesign({ checks: checks.filter((_, i) => i !== n) }) }, "Delete")))))))
      : h("p", { class: "empty" }, "No checks yet."),
    editing ? checkEditor(editing) : h("button", { class: "btn quiet", onclick: () => startEditing("check") }, "Add a check"),
    suggestions.length ? h("div", { class: "section" }, h("h2", {}, "Suggested from the recording"),
      h("ul", { class: "problems" }, suggestions.map(x => h("li", {},
        `${itemLabel(x.item)}: ${CHECK_TYPES.find(([k]) => k === x.type)[1].toLowerCase()} `,
        h("code", {}, checkSummary({ ...x, expected: x.expected ?? null })), " ",
        h("button", { class: "btn quiet small", onclick: () => saveDesign({ checks: [...checks, x] }) }, "Add"))))) : null,
  ];
}

// ---- Run-time settings ----------------------------------------------------------------------

function settingsTab() {
  const st = state.scriptView.design.settings, tt = st.think_time, pc = st.pacing;
  const har = state.scriptView.source === "har";
  const radio = (name, value, current, ...label) => h("label", { class: "check" },
    h("input", { type: "radio", name, value, checked: value === current }), ...label);
  const num = (name, value, attrs = {}) => h("input", { type: "number", name, value, min: 0, step: "any", class: "short", ...attrs });
  return h("form", { class: "settings", onsubmit: (e) => {
      e.preventDefault();
      const el = e.target.elements, n = (name) => +el[name].value;
      saveDesign({ settings: {
        think_time: { mode: el.tt_mode.value, multiplier: n("tt_multiplier"), cap_seconds: n("tt_cap"), seconds: n("tt_seconds"),
          minimum: n("tt_min"), maximum: n("tt_max") },
        pacing: { mode: el.pc_mode.value, seconds: n("pc_seconds") || 1, minimum: n("pc_min"), maximum: n("pc_max") },
        on_error: el.on_error.value, new_user_each_iteration: el.new_user.checked, timeout_seconds: n("timeout"),
        status_checks: el.status_checks.checked } });
    } },
    h("fieldset", {}, h("legend", {}, "Think time between business functions"),
      radio("tt_mode", "recorded", tt.mode, har ? "As recorded, times " : "Keep the plan's own timers",
        har ? num("tt_multiplier", tt.multiplier, { "aria-label": "Multiplier" }) : null, har ? ", at most " : null,
        har ? num("tt_cap", tt.cap_seconds, { "aria-label": "Longest pause in seconds" }) : null, har ? " s" : null),
      har ? null : h("input", { type: "hidden", name: "tt_multiplier", value: tt.multiplier }),
      har ? null : h("input", { type: "hidden", name: "tt_cap", value: tt.cap_seconds }),
      radio("tt_mode", "ignore", tt.mode, "None"),
      radio("tt_mode", "fixed", tt.mode, "Always ", num("tt_seconds", tt.seconds, { "aria-label": "Seconds" }), " s"),
      radio("tt_mode", "random", tt.mode, "Random between ", num("tt_min", tt.minimum, { "aria-label": "Minimum seconds" }),
        " and ", num("tt_max", tt.maximum, { "aria-label": "Maximum seconds" }), " s")),
    h("fieldset", {}, h("legend", {}, "Pacing: when the next iteration starts"),
      radio("pc_mode", "none", pc.mode, "As soon as the previous one ends"),
      radio("pc_mode", "interval", pc.mode, "Start one every ", num("pc_seconds", pc.seconds, { min: 0.1, "aria-label": "Interval in seconds" }), " s"),
      radio("pc_mode", "after_fixed", pc.mode, "Wait the same number of seconds after each iteration (uses the interval above)"),
      radio("pc_mode", "after_random", pc.mode, "Wait a random ", num("pc_min", pc.minimum, { "aria-label": "Minimum seconds" }),
        " to ", num("pc_max", pc.maximum, { "aria-label": "Maximum seconds" }), " s after each iteration")),
    h("fieldset", {}, h("legend", {}, "Errors and sessions"),
      field("When a request fails", choice("on_error", [["continue", "Carry on with the next request"],
        ["next_iteration", "Start the next iteration"], ["stop_user", "Stop that user"]], st.on_error)),
      h("label", { class: "check" }, h("input", { type: "checkbox", name: "new_user", checked: st.new_user_each_iteration }),
        "Each iteration is a new visitor (cookies are cleared)"),
      field("Timeout (seconds)", num("timeout", st.timeout_seconds, { min: 1, max: 600 })),
      h("label", { class: "check" }, h("input", { type: "checkbox", name: "status_checks", checked: st.status_checks, disabled: !har }),
        "Check that each request returns its recorded status code")),
    h("p", { class: "empty" }, "A debug replay always runs one iteration without think time or pacing."),
    h("button", { class: "btn", type: "submit" }, "Save settings"));
}
