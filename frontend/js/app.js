"use strict";
// Navigation: the address hash names the screen, so refresh and the back button keep your place.
//   #/quick[/<test id>]                         quick test and its results
//   #/scripts[/<id>[/<tab>[/<debug run id>]]]   imported scripts
//   #/scenarios[/new | /<id>[/run/<run id>]]    scenarios and their runs
//   #/scenarios/<id>/compare/<a>/<b>, #/quick/<a>/compare/<b>    two runs side by side
//   #/team, #/account                           users and projects; your password and API tokens

const SECTIONS = ["quick", "scripts", "scenarios", "infra", "team", "account"];

function showSection(name) {
  state.section = name;
  for (const section of SECTIONS) {
    const el = document.getElementById(`sec-${section}`);
    if (el) el.hidden = section !== name;
  }
  document.querySelectorAll(".sections a").forEach(a =>
    a.setAttribute("aria-current", a.dataset.section === name ? "page" : "false"));
}

function route() {
  clearTimeout(state.timer);
  if (!state.me || !state.me.authenticated) { showSignIn(); return; }
  const [section = "quick", id, tab, sub, extra] = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  showSection(SECTIONS.includes(section) ? section : "quick");
  if (tab === "compare" && state.section === "scenarios" && sub && extra) {
    openCompare(sub, extra, `#/scenarios/${id}/run/${sub}`); return;
  }
  if (tab === "compare" && state.section === "quick" && sub) {
    openCompare(id, sub, `#/quick/${id}`); return;
  }
  if (state.section === "scripts") {
    if (id) openScript(id, tab || "requests", sub || null); else renderScriptsHome();
    return;
  }
  state.script = null;
  if (state.section === "infra") { openInfra(); return; }
  if (state.section === "team") { openTeam(); return; }
  if (state.section === "account") { openAccount(); return; }
  if (state.section === "scenarios") {
    if (id && tab === "run" && sub) openScenarioRun(id, sub);
    else if (id) openScenario(id);
    else renderScenariosHome();
    return;
  }
  if (id) state.selected = id;
  refreshRuns().catch(() => {});
  showRun();
}

// After the first page load and after every sign-in.
async function enterConsole() {
  chooseProject();
  renderUserBar();
  try {
    await refreshRuns();
    await refreshScripts();
    await refreshScenarios();
  } catch (e) {
    if (e.status !== 401) $("#main").replaceChildren(h("div", { class: "notice" }, `Cannot load your work: ${e.message}`));
    return;
  }
  if (!location.hash && state.runs.length) state.selected = state.runs[0].id;
  route();
}

async function boot() {
  try {
    state.me = await api("/auth/me");
  } catch (e) {
    $("#main").replaceChildren(h("div", { class: "notice" }, `Cannot reach the server: ${e.message}`));
    return;
  }
  if (!state.me.authenticated) { showSignIn(); return; }
  await enterConsole();
}

window.addEventListener("hashchange", route);
boot();
