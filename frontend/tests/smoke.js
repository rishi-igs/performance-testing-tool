"use strict";
// End-to-end smoke test of the dashboard: the real backend over HTTP, the real page scripts,
// and a small fake DOM in place of a browser. Run by backend/tests/test_ui_smoke.py:
//     node smoke.js <base url> <script id>
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const [BASE, SCRIPT_ID] = process.argv.slice(2);
const failures = [];
const check = (cond, msg) => { if (cond) console.log("ok   ", msg); else { failures.push(msg); console.log("FAIL ", msg); } };

// ---- a fake DOM: just what the page scripts use ---------------------------------------------
class Text { constructor(t) { this.nodeType = 3; this.data = String(t); this.parent = null; }
  get textContent() { return this.data; } }
function* descendants(node) { for (const c of node.children || []) { if (c.nodeType === 1) { yield c; yield* descendants(c); } } }
const camel = (s) => s.replace(/-(.)/g, (_, c) => c.toUpperCase());

class El {
  constructor(tag) { this.tag = tag.toLowerCase(); this.nodeType = 1; this.children = []; this.attrs = {}; this.listeners = {};
    this.dataset = {}; this.parent = null; this._value = undefined; this._checked = undefined; this.hidden = false; this.files = []; }
  set className(v) { this.attrs.class = v; }
  get className() { return this.attrs.class || ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith("data-")) this.dataset[camel(k.slice(5))] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  get id() { return this.attrs.id; }
  get type() { return this.attrs.type || (this.tag === "select" ? "select-one" : this.tag === "textarea" ? "textarea" : "text"); }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  append(...kids) { for (const k of kids) { k.parent = this; this.children.push(k); } }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  get textContent() { return this.children.map(c => c.textContent).join(""); }
  set textContent(v) { this.children = []; if (v !== "") this.append(new Text(v)); }
  get value() {
    if (this._value !== undefined) return this._value;
    if (this.tag === "textarea") return this.textContent;
    if (this.tag === "select") {
      const options = [...descendants(this)].filter(e => e.tag === "option");
      const chosen = options.find(o => "selected" in o.attrs) || options[0];
      return chosen ? chosen.value : "";
    }
    if (this.tag === "option") return "value" in this.attrs ? this.attrs.value : this.textContent;
    return this.attrs.value ?? "";
  }
  set value(v) { this._value = String(v); }
  get checked() { return this._checked !== undefined ? this._checked : "checked" in this.attrs; }
  set checked(v) { this._checked = !!v; }
  get form() { let p = this.parent; while (p && p.tag !== "form") p = p.parent; return p; }
  get elements() {
    const named = [...descendants(this)].filter(e => e.attrs.name);
    const out = {};
    for (const e of named) {
      const same = named.filter(x => x.attrs.name === e.attrs.name);
      out[e.attrs.name] = same.length > 1 ? { value: (same.find(x => x.checked) || {}).value || "", length: same.length } : e;
    }
    return out;
  }
  focus() { document.activeElement = this; }
  setSelectionRange() {}
  reset() { for (const e of descendants(this)) { e._value = undefined; e._checked = undefined; } }
}

const root = new El("body");
const add = (parent, tag, attrs = {}) => { const e = new El(tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); parent.append(e); return e; };
const userbar = add(root, "div", { id: "userbar" });
const nav = add(root, "nav", { class: "sections" });
add(nav, "a", { "data-section": "quick", href: "#/quick" });
add(nav, "a", { "data-section": "scripts", href: "#/scripts" });
add(nav, "a", { "data-section": "scenarios", href: "#/scenarios" });
add(nav, "a", { "data-section": "infra", href: "#/infra" });
const navTeam = add(nav, "a", { "data-section": "team", href: "#/team", id: "nav-team" });
navTeam.hidden = true;
const quick = add(root, "section", { id: "sec-quick" });
const form = add(quick, "form", { id: "form" });
add(form, "button", { id: "start", type: "submit" });
add(form, "div", { id: "formerror" });
add(quick, "div", { id: "runs" });
const scripts = add(root, "section", { id: "sec-scripts" });
const importForm = add(scripts, "form", { id: "importform" });
add(importForm, "input", { name: "file", type: "file" });
add(importForm, "button", { id: "importbtn", type: "submit" });
add(importForm, "div", { id: "importerror" });
add(scripts, "div", { id: "scripts" });
const scenarioRail = add(root, "section", { id: "sec-scenarios" });
add(scenarioRail, "a", { id: "newscenario", href: "#/scenarios/new" });
add(scenarioRail, "div", { id: "scenarios" });
add(root, "section", { id: "sec-team" });
add(root, "section", { id: "sec-account" });
const main = add(root, "main", { id: "main" });

function query(sel) {
  const all = [...descendants(root)];
  let m;
  if ((m = sel.match(/^#(.+)$/))) return all.filter(e => e.attrs.id === m[1]);
  if ((m = sel.match(/^\[data-key="(.+)"\]$/))) return all.filter(e => e.dataset.key === m[1]);
  if (sel === ".sections a") return [...descendants(nav)].filter(e => e.tag === "a");
  throw new Error(`selector not supported by the fake DOM: ${sel}`);
}
global.document = {
  createElement: (t) => new El(t), createTextNode: (t) => new Text(t),
  getElementById: (id) => query(`#${id}`)[0] || null,
  querySelector: (sel) => query(sel)[0] || null, querySelectorAll: (sel) => query(sel),
  activeElement: null, cookie: "",
};
const hashListeners = [];
let hash = `#/scripts/${SCRIPT_ID}`;
global.location = {
  get hash() { return hash; },
  set hash(v) { if (v !== hash) { hash = v; setTimeout(() => hashListeners.forEach(f => f()), 0); } },
};
global.window = { scrollY: 0, scrollTo() {}, addEventListener: (t, f) => { if (t === "hashchange") hashListeners.push(f); } };
global.confirm = () => true;
global.alert = (m) => { failures.push(`alert: ${m}`); };
global.Chart = class { destroy() {} update() {} };
const realFetch = globalThis.fetch;
const calls = [];
// The browser's cookie jar, so a sign-in lasts from one request to the next.
const jar = {};
async function withCookies(p, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const cookie = Object.entries(jar).map(([k, v]) => `${k}=${v}`).join("; ");
  if (cookie) headers.Cookie = cookie;
  const res = await realFetch(new URL(p, BASE), { ...opts, headers });
  for (const line of res.headers.getSetCookie()) {
    const [pair] = line.split(";");
    const name = pair.slice(0, pair.indexOf("=")).trim(), value = pair.slice(pair.indexOf("=") + 1).replace(/^"|"$/g, "");
    if (!value || /max-age=0/i.test(line)) delete jar[name]; else jar[name] = value;
  }
  return res;
}
global.fetch = (p, opts = {}) => { calls.push(`${opts.method || "GET"} ${p}`); return withCookies(p, opts); };

// ---- interaction helpers --------------------------------------------------------------------
const text = (node) => node.textContent;
const within = (node, pred) => [...descendants(node)].filter(pred);
const byText = (node, tag, label) => within(node, e => e.tag === tag && text(e).trim() === label)[0];
const named = (node, name) => within(node, e => e.attrs.name === name);
function fire(el, type, props = {}) {
  Object.assign(el, props);
  for (const f of el.listeners[type] || []) f({ target: el, currentTarget: el, preventDefault() {} });
}
function click(el) {
  if (!el) throw new Error("nothing to click");
  fire(el, "click");
  const t = el.attrs.type;
  if (el.tag === "button" && (t === "submit" || t == null) && el.form) fire(el.form, "submit");
}
async function waitFor(cond, what, timeout = 20000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    try { if (cond()) return true; } catch (_) {}
    await new Promise(r => setTimeout(r, 50));
  }
  check(false, `timed out waiting for: ${what}`);
  return false;
}
const go = (h) => { location.hash = h; };
const json = async (p) => (await withCookies(p)).json();

process.on("unhandledRejection", (e) => { failures.push(`unhandled: ${e && e.stack || e}`); });

// ---- the scenario ---------------------------------------------------------------------------
(async () => {
  for (const file of ["core.js", "quick.js", "scripts.js", "design.js", "debug.js", "scenarios.js", "results.js", "analysis.js",
    "infra.js", "schedules.js", "team.js", "app.js"]) {
    vm.runInThisContext(fs.readFileSync(path.join(__dirname, "..", "js", file), "utf8"), { filename: file });
  }
  const sid = SCRIPT_ID;

  await waitFor(() => text(main).includes("10 captured, 10 kept"), "the Requests tab shows the import summary");
  check(text(document.getElementById("scripts")).includes("REQUESTS") || text(document.getElementById("scripts")).includes("requests"), "the rail lists imported scripts");
  check(!scripts.hidden && quick.hidden, "the Scripts section is shown");

  go(`#/scripts/${sid}/correlation`);
  await waitFor(() => text(main).includes("found automatically"), "the Correlation tab lists rules");
  const rows = within(main, e => e.tag === "tr" && text(e).includes("found automatically"));
  check(rows.length === 5, `five automatic correlations are listed (${rows.length})`);
  const tokenRow = rows.find(r => text(r).includes("${token}"));
  click(byText(tokenRow, "button", "Edit"));
  await waitFor(() => within(main, e => e.tag === "form" && e.className === "editor").length === 1, "the correlation editor opens");
  const editor = within(main, e => e.tag === "form" && e.className === "editor")[0];
  named(editor, "expression")[0].value = "$['token']";
  click(byText(editor, "button", "Save correlation"));
  await waitFor(() => text(main).includes("$['token']") && !within(main, e => e.className === "editor").length, "the edited rule is saved and shown");
  check((await json(`/scripts/${sid}`)).design.correlations.some(c => c.variable === "token" && c.expression === "$['token']"),
    "the server stored the edited expression");

  go(`#/scripts/${sid}/parameters`);
  await waitFor(() => text(main).includes("Generated values"), "the Parameters tab opens");
  click(byText(main, "button", "Add a parameter"));
  await waitFor(() => within(main, e => e.className === "editor").length === 1, "the parameter editor opens");
  let pform = within(main, e => e.className === "editor")[0];
  named(pform, "name")[0].value = "city";
  named(pform, "list_values")[0].value = "Paris\nOslo";
  click(byText(pform, "button", "Save parameter"));
  await waitFor(() => text(main).includes("${city}") && text(main).includes("2 values"), "the list parameter is saved");
  const replaceForm = within(main, e => e.tag === "form" && e.className === "inline" && named(e, "find").length)[0];
  named(replaceForm, "find")[0].value = "book";
  named(replaceForm, "variable")[0].value = "city";
  click(byText(replaceForm, "button", "Replace"));
  await waitFor(() => within(main, e => e.tag === "code" && text(e) === "book").length === 1, "the replacement is listed");

  click(byText(main, "button", "Add a parameter"));
  await waitFor(() => within(main, e => e.className === "editor").length === 1, "the editor opens again");
  pform = within(main, e => e.className === "editor")[0];
  named(pform, "name")[0].value = "stamp";
  fire(named(pform, "type")[0], "change", { value: "date" });
  await waitFor(() => within(main, e => e.attrs.name === "offset_days").length === 1, "changing the type shows the date fields");
  pform = within(main, e => e.className === "editor")[0];
  check(named(pform, "name")[0].value === "stamp", "the typed name survives the type change");
  click(byText(pform, "button", "Cancel"));

  go(`#/scripts/${sid}/checks`);
  await waitFor(() => text(main).includes("Add a check"), "the Checks tab opens");
  click(byText(main, "button", "Add a check"));
  await waitFor(() => within(main, e => e.className === "editor").length === 1, "the check editor opens");
  const cform = within(main, e => e.className === "editor")[0];
  named(cform, "target")[0].value = "1";
  named(cform, "value")[0].value = '"ok"';
  click(byText(cform, "button", "Add check"));
  await waitFor(() => text(main).includes("Response contains text") && text(main).includes('"\"ok\""'), "the check is listed");

  go(`#/scripts/${sid}/settings`);
  await waitFor(() => text(main).includes("Pacing"), "the Run-time settings tab opens");
  const sform = within(main, e => e.tag === "form" && e.className === "settings")[0];
  for (const r of named(sform, "tt_mode")) r.checked = r.attrs.value === "fixed";
  named(sform, "tt_seconds")[0].value = "2";
  click(byText(sform, "button", "Save settings"));
  await waitFor(() => text(document.getElementById("savestate") || main) === "Saved", "settings are saved");
  const saved = (await json(`/scripts/${sid}`)).design.settings.think_time;
  check(saved.mode === "fixed" && saved.seconds === 2, "the server stored fixed 2 s think time");

  go(`#/scripts/${sid}/debug`);
  await waitFor(() => text(main).includes("No replays yet"), "the Debug tab opens");
  click(byText(main, "button", "Run debug replay"));
  await waitFor(() => text(main).includes("Passed: all 10 requests behaved as recorded."), "the replay passes", 60000);
  check(location.hash.startsWith(`#/scripts/${sid}/debug/d_`), "the address names the debug run");
  check(text(main).includes("Values captured") && text(main).includes("${XSRF_TOKEN}"), "captured values are listed");
  click(within(main, e => e.tag === "button" && text(e).startsWith("#1 POST /page/submit"))[0]);
  await waitFor(() => text(main).includes("Response: 200"), "a request's details open");
  check(text(main).includes("csrf=") && text(main).includes("item=Paris"), "the details show the request sent, with the parameter value");
  check(text(main).includes("Passed: Check: text"), "the check result is shown");

  // ---- correlate automatically: this HAR already has every rule, so one replay confirms it ----
  const autoButton = () => within(main, e => e.tag === "button" && text(e) === "Correlate automatically")[0];
  go(`#/scripts/${sid}/correlation`);
  await waitFor(() => !!autoButton(), "the Correlation tab offers to correlate automatically");
  click(autoButton());
  await waitFor(() => text(main).includes("No new correlations were needed in 1 replay. The last replay passed."),
    "automatic correlation replays once and finds nothing missing", 60000);
  check(within(main, e => e.tag === "tr" && text(e).includes("found automatically")).length === 5, "the five rules are unchanged");

  // ---- infrastructure: a monitor and a load generator ----
  const rmi = require("net").createServer((sock) => sock.end());
  await new Promise(r => rmi.listen(0, "127.0.0.1", r));
  go("#/infra");
  await waitFor(() => text(main).includes("Load generators and monitors"), "the Infrastructure page opens");
  const monForm = within(main, e => e.tag === "form" && named(e, "kind").length)[0];
  named(monForm, "name")[0].value = "console";
  named(monForm, "kind")[0].value = "local";
  click(byText(monForm, "button", "Add monitor"));
  await waitFor(() => within(main, e => e.tag === "tr" && text(e).includes("This machine")).length === 1, "the monitor is listed");
  click(within(main, e => e.tag === "tr" && text(e).includes("This machine")).map(r => byText(r, "button", "Check"))[0]);
  await waitFor(() => within(main, e => e.tag === "tr" && text(e).includes("This machine") && text(e).includes("reachable")).length === 1,
    "checking the monitor shows its values");
  check(/CPU \d/.test(text(main)), "the monitor reports CPU");
  const genForm = within(main, e => e.tag === "form" && named(e, "host").length)[0];
  named(genForm, "name")[0].value = "lg-test";
  named(genForm, "host")[0].value = "127.0.0.1";
  named(genForm, "port")[0].value = String(rmi.address().port);
  click(byText(genForm, "button", "Add generator"));
  await waitFor(() => within(main, e => e.tag === "tr" && text(e).includes("lg-test")).length === 1, "the generator is listed");
  click(within(main, e => e.tag === "tr" && text(e).includes("lg-test")).map(r => byText(r, "button", "Check"))[0]);
  await waitFor(() => within(main, e => e.tag === "tr" && text(e).includes("lg-test") && text(e).includes("reachable")).length === 1,
    "checking the generator connects to it");
  rmi.close();

  // ---- a scenario: build it in the editor, run it, read the results ----
  go("#/scenarios/new");
  await waitFor(() => text(main).includes("Schedule preview") && within(main, e => e.attrs["aria-label"] === "Group name" && e.value === "Group 1").length === 1,
    "the scenario editor opens with a first group");
  check(!scenarioRail.hidden && scripts.hidden, "the Scenarios section is shown");
  const users = within(main, e => e.attrs["aria-label"] === "Users in Group 1")[0];
  fire(users, "input", { value: "2" });
  fire(within(main, e => e.attrs["aria-label"] === "Think time of Group 1")[0], "change", { value: "ignore" });
  fire(within(main, e => e.dataset.key === "s-ramp")[0], "input", { value: "1" });
  fire(within(main, e => e.dataset.key === "s-hold")[0], "input", { value: "3" });
  fire(within(main, e => e.dataset.key === "sc-name")[0], "input", { value: "Smoke scenario" });
  click(byText(main, "button", "Add an SLA"));
  await waitFor(() => within(main, e => e.attrs["aria-label"] === "Limit").length === 1, "an SLA row is added");
  fire(within(main, e => e.attrs["aria-label"] === "Limit")[0], "input", { value: "5000" });
  const monitorId = (await json("/monitors"))[0].id;
  const pick = within(main, e => e.dataset.key === `pick-${monitorId}`)[0];
  check(!!pick, "the editor offers the monitor");
  fire(pick, "change", { checked: true });
  click(byText(main, "button", "Save and run"));
  await waitFor(() => /#\/scenarios\/c_[^/]+\/run\/t_/.test(location.hash), "saving and running opens the run view");
  await waitFor(() => text(main).includes("Transaction summary"), "the run view renders");
  await waitFor(() => text(main).includes("Passed:") && /all \d+ SLA checks met/.test(text(main)), "the run passes its SLA (checked per business function)", 60000);
  check(text(main).includes("Login") && text(main).includes("Page Form"), "the transaction summary lists business functions");
  check(text(main).includes("Running users per group") && text(main).includes("Groups"), "group results are shown");
  const savedScenario = (await json("/scenarios")).find(s => s.name === "Smoke scenario");
  check(!!savedScenario, "the scenario was saved with the edited name");
  const stored = await json(`/scenarios/${savedScenario.id}`);
  check(stored.config.groups[0].users === 2 && stored.config.groups[0].settings.think_time.mode === "ignore"
    && stored.config.schedule.duration_seconds === 3 && stored.config.sla[0].limit === 5000, "the editor's values reached the server");
  check(stored.config.monitors.length === 1 && stored.config.monitors[0] === monitorId, "the monitor was saved with the scenario");
  go(`#/scenarios/${savedScenario.id}`);
  await waitFor(() => text(main).includes("passed") && text(main).includes("Planned users"), "the scenario lists its run and the planned users");

  // ---- analysis, comparison and reports of the finished run ----
  const firstRun = stored.runs[0].id;
  const second = await (await realFetch(new URL(`/scenarios/${savedScenario.id}/run`, BASE), { method: "POST" })).json();
  await waitFor(() => { json(`/tests/${second.id}`).then(r => { second.status = r.status; }); return second.status && second.status !== "running"; },
    "a second run finishes", 60000);
  go(`#/scenarios/${savedScenario.id}/run/${firstRun}`);
  await waitFor(() => text(main).includes("Analysis") && within(main, e => e.dataset.key === "a-users").length === 1, "the graph builder appears");
  check(within(main, e => e.dataset.key === "a-users")[0].checked, "running users are shown by default");
  fire(within(main, e => e.dataset.key === "a-hits")[0], "change", { checked: true });
  await waitFor(() => within(main, e => e.dataset.key === "a-hits")[0].checked, "ticking another graph adds it");
  check(within(main, e => e.tag === "a" && (e.attrs.href || "") === `/reports/${firstRun}/summary.pdf`).length === 1, "the PDF report is offered");
  const compareForm = within(main, e => e.tag === "form" && named(e, "other").length)[0];
  click(byText(compareForm, "button", "Compare"));
  await waitFor(() => text(main).includes("Run comparison"), "the comparison opens");
  check(text(main).includes("Login") && text(main).includes("A (baseline)"), "the comparison lists business functions for both runs");

  // ---- trends, the baseline and a scheduled run, on the scenario page ----
  const useAsBaseline = () => within(main, e => e.tag === "button" && text(e) === "Use as baseline");
  go(`#/scenarios/${savedScenario.id}`);
  await waitFor(() => text(main).includes("Trends") && useAsBaseline().length === 2, "the scenario's two finished runs are listed under Trends");
  click(useAsBaseline()[1]);                                       // rows are newest first: this is the first run
  await waitFor(() => /in line with the baseline|worse than the baseline/.test(text(main)), "the latest run is checked against the baseline");
  check((await json(`/scenarios/${savedScenario.id}`)).baseline_run_id === firstRun, "the server stored the first run as the baseline");
  const scheduleForm = () => within(main, e => e.tag === "form" && e.attrs["aria-label"] === "Add a scheduled run")[0];
  fire(named(scheduleForm(), "repeat")[0], "change", { value: "weekly" });
  await waitFor(() => within(main, e => e.attrs.name === "day0").length === 1, "a weekly schedule asks for the days");
  named(scheduleForm(), "time")[0].value = "03:30";
  named(scheduleForm(), "day1")[0].checked = true;
  named(scheduleForm(), "day3")[0].checked = true;
  click(byText(scheduleForm(), "button", "Add schedule"));
  await waitFor(() => text(main).includes("Every Tue, Thu at 03:30"), "the weekly schedule is listed");
  const schedules = await json(`/schedules?scenario_id=${savedScenario.id}`);
  check(schedules.length === 1 && schedules[0].config.days.join() === "1,3" && !!schedules[0].next_run_at,
    "the server stored the schedule and its next run");

  // ---- accounts: the first administrator, a tester, signing out and in again ----
  check(text(userbar).includes("Open access"), "the rail says the console is open to anyone");
  go("#/team");
  await waitFor(() => text(main).includes("Create the first administrator"), "the Team page offers to create the first administrator");
  const setup = within(main, e => e.tag === "form" && text(e).includes("Create the first administrator"))[0];
  named(setup, "username")[0].value = "Chief";
  named(setup, "password")[0].value = "smoke-admin-password";
  named(setup, "confirm")[0].value = "smoke-admin-password";
  click(byText(setup, "button", "Create and sign in"));
  await waitFor(() => text(userbar).includes("chief") && within(main, e => e.tag === "form" && e.attrs["aria-label"] === "Add a user").length === 1,
    "creating the administrator signs you in and opens the user list");
  check(!navTeam.hidden && !!jar.session, "the administrator has a session and sees the Team section");
  const addUser = within(main, e => e.tag === "form" && e.attrs["aria-label"] === "Add a user")[0];
  named(addUser, "username")[0].value = "tess";
  named(addUser, "password")[0].value = "smoke-tester-password";
  click(byText(addUser, "button", "Add user"));
  await waitFor(() => within(main, e => e.tag === "tr" && e.children.length && text(e.children[0]) === "tess").length === 1,
    "the new tester is listed");
  check(text(main).includes("tess") && within(main, e => e.tag === "td" && text(e) === "tess").length === 2,
    "the tester appears in the user list and as a member of the Default project");
  const tess = (await json("/users")).find(u => u.username === "tess");
  check(tess && tess.role === "tester" && tess.projects.join() === "p_default", "the tester was created in the Default project");

  go("#/account");
  await waitFor(() => text(main).includes("API tokens"), "your account page opens");
  const tokenForm = within(main, e => e.tag === "form" && e.attrs["aria-label"] === "Create an API token")[0];
  named(tokenForm, "name")[0].value = "smoke-ci";
  click(byText(tokenForm, "button", "Create token"));
  await waitFor(() => within(main, e => e.tag === "code" && text(e).startsWith("pt_")).length === 1, "the new token is shown once");

  click(within(userbar, e => e.tag === "button" && text(e) === "Sign out")[0]);
  await waitFor(() => !!document.getElementById("loginform"), "signing out shows the sign-in form");
  check(!jar.session && text(document.getElementById("scripts")) === "", "the session cookie is gone and the rail is cleared");
  const loginForm = document.getElementById("loginform");
  named(loginForm, "username")[0].value = "tess";
  named(loginForm, "password")[0].value = "not-the-password";
  click(byText(loginForm, "button", "Sign in"));
  await waitFor(() => text(document.getElementById("loginerror")) === "Wrong user name or password", "a wrong password is reported");
  named(loginForm, "password")[0].value = "smoke-tester-password";
  click(byText(loginForm, "button", "Sign in"));
  await waitFor(() => text(userbar).includes("tess") && text(document.getElementById("scripts")).length > 0,
    "the tester signs in and sees the project's scripts");
  check(navTeam.hidden, "testers do not see the Team section");
  go("#/infra");
  await waitFor(() => text(main).includes("Administrators add and remove load generators."), "testers cannot change the infrastructure");

  const unexpected = calls.filter(c => /undefined|null/.test(c));
  check(!unexpected.length, `no request URL contains undefined or null (${unexpected.join(", ")})`);
  console.log(failures.length ? `\n${failures.length} FAILED` : "\nall UI checks passed");
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
