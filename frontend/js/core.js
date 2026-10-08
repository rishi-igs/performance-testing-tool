"use strict";
// Shared helpers for every screen of the console.

const $ = (sel) => document.querySelector(sel);
const state = {
  section: "quick", selected: null, runs: [], charts: {}, timer: null,
  script: null, scripts: [], scriptView: null, scriptTab: "requests", rulesOpen: false,
  filters: { q: "", method: "", domain: "", showExcluded: false }, exportOpts: { users: 1, loops: 1, ramp: 0 },
  editing: null, debugRuns: [], debugRun: null, debugSample: null, debugReveal: false, debugFailedOnly: false,
  scenarios: [], scenarioId: null, scenario: null, draft: null, preview: null, scriptInfo: {}, runId: null,
  analysis: null, me: null, project: null, team: null, tokens: [], schedules: [], trends: null, autoJob: null,
  scheduleDraft: { repeat: "daily" },
};

// Roles, lowest first: viewers read, testers build and run, administrators manage the team.
const ROLE_RANK = { viewer: 1, tester: 2, admin: 3 };
const can = (role) => !state.me || (ROLE_RANK[state.me.role] || 0) >= ROLE_RANK[role];

// Lists and new items belong to the project chosen in the rail.
function inProject(path) {
  if (!state.project) return path;
  return `${path}${path.includes("?") ? "&" : "?"}project=${encodeURIComponent(state.project)}`;
}

// Build DOM with textContent only, so names and error text from the server cannot inject HTML.
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
}
const fmt = (n, d = 0) => (n == null ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const fmtBytes = (n) => n < 1024 ? `${fmt(n)} B` : n < 1048576 ? `${fmt(n / 1024, 1)} KB` : `${fmt(n / 1048576, 1)} MB`;
const listOf = (text) => text.split(/[\s,]+/).map(s => s.trim()).filter(Boolean);
const pad2 = (n) => String(n).padStart(2, "0");
const varRef = (name) => "${" + name + "}";

function ago(iso) {
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function urlParts(u) {
  try { const x = new URL(u); return { host: x.host, path: x.pathname + x.search }; }
  catch (_) { return { host: "", path: u }; }
}

// The X-Requested-With header tells the server this write comes from the console itself:
// other sites cannot add it, so they cannot act with your session.
async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts,
    headers: { "Content-Type": "application/json", "X-Requested-With": "console", ...(opts.headers || {}) } });
  if (res.status === 401 && path !== "/auth/login" && path !== "/auth/me") {     // those two expect it
    showSignIn("Your session has ended. Sign in again.");
    const err = new Error("Sign in to continue"); err.status = 401; throw err;
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = Array.isArray(j.detail) ? j.detail.map(d => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("\n") : (j.detail || detail);
    } catch (_) {}
    const err = new Error(detail); err.status = res.status; throw err;
  }
  return res.status === 204 ? null : res.json();
}

const setMain = (...nodes) => $("#main").replaceChildren(...nodes.flat().filter(n => n != null && n !== false));

function clearCharts() { Object.values(state.charts).forEach(ch => ch.destroy()); state.charts = {}; }

function go(hash) {
  if (location.hash === hash) route(); else location.hash = hash;
}

// Re-rendering replaces the DOM; put the keyboard focus back where it was.
function restoreFocus(key) {
  const target = key && document.querySelector(`[data-key="${key}"]`);
  if (!target) return;
  target.focus({ preventScroll: true });
  if (target.type === "search" || target.type === "text") target.setSelectionRange(target.value.length, target.value.length);
}
const focusedKey = () => document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : null;

function field(label, input, hint) {
  return h("label", {}, label, input, hint ? h("small", {}, hint) : null);
}
function choice(name, options, current, attrs = {}) {
  return h("select", { name, ...attrs }, options.map(([value, label]) =>
    h("option", { value, selected: String(value) === String(current) }, label)));
}

function stat(value, label) { return h("div", { class: "stat" }, h("div", { class: "v" }, value), h("div", { class: "l" }, label)); }
