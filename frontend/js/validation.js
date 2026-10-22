"use strict";
// Validation tab: the HAR comparison validator (requirements 8.5). The plan is compared with the
// recording request by request, as you would in the browser's DevTools; the same check runs
// before every debug replay and every scenario run.

const DIFF_LABEL = { missing: "missing", extra: "extra", changed: "changed", unresolved: "unresolved", variable: "variable" };

async function loadValidation(focusKey) {
  const script = state.script;
  try {
    const data = await api(`/scripts/${script}/validation${state.validationReveal ? "?reveal=true" : ""}`);
    state.validationReport = { script, data };
  } catch (e) { state.validationReport = { script, error: e.message }; }
  if (state.script !== script || state.scriptTab !== "validation") return;
  renderScriptPage(focusKey);
}

function variableNameFor(name) {
  const base = String(name || "value").replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "value";
  return /^\d/.test(base) ? `v_${base}` : base;
}

function unresolvedValuesSection(report) {
  const d = state.scriptView.design;
  const values = report.unresolved_values;
  if (!values.length) return null;
  return h("div", { class: "section" }, h("h2", {}, "Dynamic values still sent as recorded"),
    h("p", { class: "intro" }, "Each looks like something the server issued or the browser generated. Correlate it, make it a parameter, or mark it static if replaying the recorded value is right."),
    h("ul", { class: "problems" }, values.map((v, n) => h("li", { class: "bad" },
      h("code", {}, v.name), ` in ${itemLabel(v.index)}: ${v.preview} `,
      can("tester") ? [
        h("button", { class: "btn quiet small", "data-key": `vv-c-${n}`, onclick: () => editIn("correlation", "correlation",
          { variable: variableNameFor(v.name), replace: v.value || "" }) }, "Correlate"), " ",
        v.value ? h("button", { class: "btn quiet small", onclick: () => {
          const name = variableNameFor(v.name);
          saveDesign({ parameters: [...d.parameters, { name, type: "list", values: [v.value] }],
            replacements: [...d.replacements, { find: v.value, variable: name }] });
        } }, "Make it a parameter") : null, " ",
        v.value ? h("button", { class: "btn quiet small", onclick: () => saveDesign({ static_values: [...d.static_values, v.value] }) },
          "Mark static") : h("small", {}, "Show recorded values to mark it static."),
      ] : null))));
}

function unresolvedVariablesSection(report) {
  if (!report.unresolved_variables.length) return null;
  return h("div", { class: "section" }, h("h2", {}, "Variables nothing sets"),
    h("ul", { class: "problems" }, report.unresolved_variables.map(u => h("li", { class: "bad" },
      h("code", {}, varRef(u.name)), ` is sent by ${u.requests.map(i => `#${i}`).join(", ") || "shared headers"}. `,
      can("tester") ? [
        h("button", { class: "btn quiet small", onclick: () => editIn("correlation", "correlation", { variable: u.name }) }, "Correlate"), " ",
        h("button", { class: "btn quiet small", onclick: () => editIn("parameters", "parameter", { name: u.name, type: "list" }) }, "Make it a parameter"),
      ] : null))));
}

function staticValuesSection() {
  const d = state.scriptView.design;
  if (!d.static_values.length) return null;
  return h("div", { class: "section" }, h("h2", {}, "Marked static"),
    h("ul", { class: "problems" }, d.static_values.map((value, n) => h("li", { class: "good" },
      h("code", {}, value.length > 40 ? `${value.slice(0, 4)}… (${value.length} characters)` : value), " ",
      can("tester") ? h("button", { class: "btn quiet small", "aria-label": `Stop treating ${value} as static`,
        onclick: () => saveDesign({ static_values: d.static_values.filter((_, i) => i !== n) }) }, "Remove") : null))));
}

function requestPane(title, req, marks) {
  const headerLine = (name, value) => {
    const mark = marks.get(name.toLowerCase());
    return h("div", { class: mark ? `diffline ${mark}` : null }, `${name}: ${value}`);
  };
  const body = req.form ? req.form.map(([n, v]) => `${n}=${v}`).join("&") : req.body;
  return h("div", { class: "pane" },
    h("h3", {}, title),
    h("p", { class: "mono" }, `${req.method} ${req.url}`),
    h("div", { class: "code headers" }, req.headers.length ? req.headers.map(([n, v]) => headerLine(n, v)) : "(no headers)"),
    body ? h("pre", { class: "code" }, prettyBody(body)) : null);
}

function differencesList(row) {
  const shown = row.differences.filter(d => d.severity !== "info" || d.type === "variable");
  if (!shown.length) return h("p", { class: "allclear" }, "Same as the recording.");
  return h("ul", { class: "problems" }, shown.map(d => h("li", { class: d.severity === "fail" ? "bad" : d.severity === "warning" ? "warnline" : "good" },
    h("span", { class: `dtype ${d.type}` }, DIFF_LABEL[d.type]), " ", d.message,
    d.type === "changed" || d.type === "variable"
      ? h("small", { class: "src" }, `recorded ${JSON.stringify(d.recorded)} · generated ${JSON.stringify(d.generated)}`) : null)));
}

function validationRow(row) {
  const open = state.validationOpen === row.index;
  const counted = row.differences.filter(d => d.severity !== "info");
  const marks = new Map(row.differences.filter(d => d.part === "header").map(d => [d.name.toLowerCase(), d.type]));
  return [
    h("tr", { class: row.status === "fail" ? "failrow" : null },
      h("td", {}, h("span", { class: `badge ${VALIDATION_BADGE[row.status]}` }, { pass: "pass", warn: "warning", fail: "fail" }[row.status])),
      h("td", {}, h("button", { class: "linklike", "aria-expanded": String(open), "data-key": `v-row-${row.index}`,
        onclick: () => { state.validationOpen = open ? null : row.index; renderScriptPage(`v-row-${row.index}`); } },
        `#${row.index} ${row.label}`)),
      h("td", {}, row.transaction || ""),
      h("td", { class: "num" }, String(row.position)),
      h("td", { class: "reason" }, counted.length ? counted.map(d => d.message).slice(0, 2).join("; ") + (counted.length > 2 ? ` (+${counted.length - 2})` : "")
        : row.differences.some(d => d.type === "variable") ? "Dynamic values sent as variables" : "")),
    open ? h("tr", { class: "detailrow" }, h("td", { colspan: 5 }, h("div", { class: "detail" },
      h("div", { class: "sidebyside" }, requestPane("Recording", row.recorded, marks), requestPane("Generated script", row.generated, marks)),
      h("div", { class: "pane" }, h("h3", {}, "Differences"), differencesList(row))))) : null,
  ];
}

function validationTab() {
  const r = state.validationReport;
  const intro = h("p", { class: "intro" }, "The generated script is compared with the recording request by request: endpoint, headers, body, dynamic values and order. It runs by itself before every debug replay and every scenario run; a script that fails cannot start a scenario.");
  if (!r || r.script !== state.script) return [intro, h("p", { class: "empty" }, "Comparing with the recording…")];
  if (r.error) return [intro, h("div", { class: "warn critical" }, r.error)];
  const report = r.data, c = report.counts;
  const rows = report.requests.filter(x => !state.validationDiffOnly || x.status !== "pass");
  const verdicts = { pass: "Every request matches the recording; dynamic values are sent as variables.",
    warn: "The script should work, but check the warnings below.",
    fail: "The script will not replay correctly until the failures below are fixed." };
  return [
    intro,
    h("p", { class: `verdict ${report.status === "fail" ? "fail" : report.status === "pass" ? "pass" : ""}` },
      h("span", { class: `badge ${VALIDATION_BADGE[report.status]}` }, report.label), " ", verdicts[report.status]),
    h("p", { class: "empty" }, `${fmt(c.requests)} requests: ${fmt(c.pass)} pass, ${fmt(c.warn)} with warnings, ${fmt(c.fail)} fail · `
      + `differences: ${fmt(c.missing)} missing, ${fmt(c.extra)} extra, ${fmt(c.changed)} changed, ${fmt(c.unresolved)} unresolved · `
      + `${fmt(c.variable)} values sent as variables`),
    report.notes.map(n => h("div", { class: "warn" }, n)),
    unresolvedVariablesSection(report),
    unresolvedValuesSection(report),
    staticValuesSection(),
    h("div", { class: "toolbar" },
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!state.validationDiffOnly, "data-key": "v-only",
        onchange: (e) => { state.validationDiffOnly = e.target.checked; renderScriptPage("v-only"); } }), "Only requests with warnings or failures"),
      can("tester") ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!state.validationReveal, "data-key": "v-reveal",
        onchange: (e) => { state.validationReveal = e.target.checked; loadValidation("v-reveal"); } }), "Show recorded values (credentials too)") : null,
      h("button", { class: "btn quiet small", onclick: () => loadValidation() }, "Compare again")),
    h("div", { class: "tablewrap" }, h("table", { class: "reqs" },
      h("thead", {}, h("tr", {}, ["", "Request", "Business function", "Runs as", "Differences"].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, rows.length ? rows.flatMap(validationRow)
        : h("tr", {}, h("td", { colspan: 5, class: "empty" }, "No request has warnings or failures."))))),
  ];
}
