"use strict";
// Who is signed in, the project you work in, the team (users and projects) and your account.

const ROLE_OPTIONS = [["viewer", "Viewer: sees results"], ["tester", "Tester: builds and runs"], ["admin", "Administrator"]];
const ROLE_NAMES = { viewer: "viewer", tester: "tester", admin: "administrator" };

// ---- sign-in --------------------------------------------------------------------------------

function apiKeyForm() {
  return h("form", { onsubmit: (e) => { e.preventDefault();
      document.cookie = `api_key=${encodeURIComponent(e.target.elements.key.value)}; path=/; SameSite=Strict`;
      boot(); } },
    field("API key", h("input", { name: "key", type: "password", required: true, autocomplete: "off" })),
    h("button", { class: "btn quiet", type: "submit" }, "Use this key"));
}

function showSignIn(message) {
  clearTimeout(state.timer);
  clearCharts();
  if (state.me) state.me = { ...state.me, authenticated: false };
  for (const id of ["runs", "scripts", "scenarios"]) { const el = document.getElementById(id); if (el) el.replaceChildren(); }
  renderUserBar();
  const me = state.me || {};
  const keyOnly = me.setup_needed;          // the server has an API key and nobody has an account yet
  $("#main").replaceChildren(h("div", { class: "welcome" },
    h("h1", {}, keyOnly ? "API key needed" : "Sign in"),
    message ? h("p", { class: "notice" }, message) : null,
    keyOnly ? [h("p", {}, "This server requires its API key. Enter it to continue."), apiKeyForm()] : [
      h("form", { id: "loginform", onsubmit: signIn },
        field("User name", h("input", { name: "username", required: true, autocomplete: "username", "data-key": "login-user" })),
        field("Password", h("input", { name: "password", type: "password", required: true, autocomplete: "current-password" })),
        h("button", { class: "btn", type: "submit" }, "Sign in"),
        h("div", { class: "formerror", id: "loginerror", role: "alert" })),
      me.api_key_enabled ? h("details", {}, h("summary", {}, "Use the API key instead"), h("div", {}, apiKeyForm())) : null]));
  restoreFocus("login-user");
}

async function signIn(e) {
  e.preventDefault();
  const el = e.target.elements, err = $("#loginerror");
  err.textContent = "";
  try {
    state.me = await api("/auth/login", { method: "POST",
      body: JSON.stringify({ username: el.username.value.trim(), password: el.password.value }) });
  } catch (ex) { err.textContent = ex.message; return; }
  await enterConsole();
}

async function signOut() {
  try { await api("/auth/logout", { method: "POST" }); } catch (_) {}
  try { state.me = await api("/auth/me"); } catch (_) { state.me = null; }
  if (state.me && state.me.authenticated) { await enterConsole(); return; }      // an API key cookie still signs you in
  showSignIn("You are signed out.");
}

function forgetKey() {
  document.cookie = "api_key=; path=/; max-age=0; SameSite=Strict";
  boot();
}

// ---- the rail: who you are, and the project you work in --------------------------------------

function chooseProject() {
  const ids = ((state.me && state.me.projects) || []).map(p => p.id);
  let saved = null;
  try { saved = localStorage.getItem("perf.project"); } catch (_) {}
  state.project = ids.includes(saved) ? saved : ids.includes("p_default") ? "p_default" : (ids[0] || null);
}

async function switchProject(id) {
  state.project = id;
  try { localStorage.setItem("perf.project", id); } catch (_) {}
  state.selected = null; state.scenarioId = null; state.script = null;
  try { await Promise.all([refreshRuns(), refreshScripts(), refreshScenarios()]); } catch (_) {}
  go(`#/${state.section}`);
}

function renderUserBar() {
  const me = state.me && state.me.authenticated ? state.me : null;
  const team = document.getElementById("nav-team");
  if (team) team.hidden = !(me && can("admin"));
  for (const id of ["start", "importbtn"]) {
    const button = document.getElementById(id);
    if (!button) continue;
    button.disabled = !!(me && !can("tester"));
    if (button.disabled) button.setAttribute("title", "Needs the tester role"); else button.removeAttribute("title");
  }
  const newScenario = document.getElementById("newscenario");
  if (newScenario) newScenario.hidden = !!(me && !can("tester"));
  const bar = document.getElementById("userbar");
  if (!bar) return;
  if (!me) { bar.replaceChildren(); return; }
  const who = me.mode === "open"
    ? [h("strong", {}, "Open access"), ": anyone who can reach this server can use it. ", h("a", { href: "#/team" }, "Set up sign-in")]
    : me.mode === "api_key"
      ? [h("strong", {}, "Signed in with the API key"), " · ", h("button", { class: "linklike", type: "button", onclick: forgetKey }, "Forget the key")]
      : [h("strong", {}, me.name), ` · ${ROLE_NAMES[me.role]} · `, h("a", { href: "#/account" }, "Your account"), " · ",
        h("button", { class: "linklike", type: "button", onclick: signOut }, "Sign out")];
  const projects = me.projects || [];
  bar.replaceChildren(...[h("p", { class: "who" }, who),
    projects.length > 1 ? field("Project", choice("project", projects.map(p => [p.id, p.name]), state.project,
      { "data-key": "project", onchange: (e) => switchProject(e.target.value) })) : null,
    projects.length ? null : h("p", { class: "empty" }, "You are not in a project yet. Ask an administrator to add you.")].filter(Boolean));
}

// ---- Team: users and projects (administrators) ------------------------------------------------

async function loadTeam() {
  const [users, projects] = await Promise.all([api("/users"), api("/projects")]);
  state.team = { users, projects };
}

async function openTeam() {
  clearTimeout(state.timer);
  clearCharts();
  if (state.me.setup_needed && state.me.mode === "open") { state.team = null; renderTeam(); return; }
  if (!can("admin")) {
    setMain(h("h1", {}, "Team"), h("div", { class: "notice" }, "Only administrators manage users and projects."));
    return;
  }
  try { await loadTeam(); } catch (e) { setMain(h("div", { class: "notice" }, e.message)); return; }
  renderTeam();
}

function teamError(text) { const el = $("#teamerror"); if (el) el.textContent = text; }

// Every change can alter your own rights and projects, so who you are is read again afterwards.
async function teamCall(path, opts, focusKey) {
  teamError("");
  try {
    await api(path, opts);
    state.me = await api("/auth/me");
    if (!(state.me.projects || []).some(p => p.id === state.project)) chooseProject();
    renderUserBar();
    if (!can("admin")) { await openTeam(); return true; }
    await loadTeam();
    renderTeam(focusKey);
    return true;
  } catch (e) { teamError(e.message); return false; }
}

function setupForm() {
  return h("form", { class: "editor", onsubmit: async (e) => {
      e.preventDefault();
      const el = e.target.elements;
      teamError("");
      if (el.password.value !== el.confirm.value) { teamError("The two passwords differ."); return; }
      try {
        state.me = await api("/auth/setup", { method: "POST",
          body: JSON.stringify({ username: el.username.value.trim(), password: el.password.value }) });
      } catch (ex) { teamError(ex.message); return; }
      await enterConsole();
    } },
    h("h3", {}, "Create the first administrator"),
    h("p", { class: "intro" }, "From then on everyone signs in, and you can add testers and viewers. Everything that exists now stays in the Default project."),
    field("User name", h("input", { name: "username", required: true, maxlength: 64, autocomplete: "username", "data-key": "setup-user" })),
    h("div", { class: "row" },
      field("Password", h("input", { name: "password", type: "password", required: true, minlength: 10, autocomplete: "new-password" }),
        "At least 10 characters"),
      field("Repeat the password", h("input", { name: "confirm", type: "password", required: true, autocomplete: "new-password" }))),
    h("button", { class: "btn", type: "submit" }, "Create and sign in"));
}

function userRow(u, projects) {
  const me = state.me;
  const put = (body, key) => teamCall(`/users/${u.id}`, { method: "PUT", body: JSON.stringify(body) }, key);
  return [
    h("tr", {},
      h("td", {}, u.username, u.id === me.user_id ? h("small", { class: "src" }, "you") : null,
        u.disabled ? h("small", { class: "src" }, "disabled: cannot sign in") : null),
      h("td", {}, choice("role", ROLE_OPTIONS, u.role, { "aria-label": `Role of ${u.username}`, "data-key": `role-${u.id}`,
        onchange: (e) => put({ role: e.target.value }, `role-${u.id}`) })),
      h("td", {}, u.role === "admin" ? h("span", { class: "empty" }, "Every project") : projects.map(p => h("label", { class: "check" },
        h("input", { type: "checkbox", checked: u.projects.includes(p.id), "data-key": `member-${u.id}-${p.id}`,
          onchange: (e) => put({ projects: e.target.checked ? [...u.projects, p.id] : u.projects.filter(x => x !== p.id) },
            `member-${u.id}-${p.id}`) }), p.name))),
      h("td", {}, u.last_login_at ? ago(u.last_login_at) : "never"),
      h("td", { class: "rowactions" },
        h("button", { class: "btn quiet small", type: "button", "aria-label": `Reset the password of ${u.username}`,
          onclick: () => { state.resetFor = state.resetFor === u.id ? null : u.id; renderTeam("reset-pw"); } }, "Reset password"),
        u.id === me.user_id ? null : h("button", { class: "btn quiet small", type: "button", onclick: () => put({ disabled: !u.disabled }) },
          u.disabled ? "Enable" : "Disable"),
        u.id === me.user_id ? null : h("button", { class: "btn quiet small", type: "button", "aria-label": `Delete ${u.username}`,
          onclick: () => confirm(`Delete the account ${u.username}? Their scripts, scenarios and runs stay.`)
            && teamCall(`/users/${u.id}`, { method: "DELETE" }) }, "Delete"))),
    state.resetFor === u.id ? h("tr", { class: "detailrow" }, h("td", { colspan: 5 },
      h("form", { class: "inline", onsubmit: async (e) => {
          e.preventDefault();
          const password = e.target.elements.password.value;
          state.resetFor = null;
          if (!(await put({ password }))) state.resetFor = u.id;
        } },
        field(`New password for ${u.username}`, h("input", { name: "password", type: "password", required: true, minlength: 10,
          autocomplete: "new-password", "data-key": "reset-pw" }), "They are signed out everywhere"),
        h("button", { class: "btn quiet", type: "submit" }, "Set password")))) : null,
  ];
}

function usersSection(users, projects) {
  return h("div", { class: "section" }, h("h2", {}, "Users"),
    h("p", { class: "intro" }, "Viewers see results and reports. Testers also import scripts, build scenarios and run tests. Administrators also manage users, projects, load generators and monitors, and see every project."),
    users.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["User", "Role", "Projects", "Last sign-in", ""].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, users.flatMap(u => userRow(u, projects))))) : h("p", { class: "empty" }, "No accounts yet."),
    h("form", { class: "inline", "aria-label": "Add a user", onsubmit: (e) => {
        e.preventDefault();
        const el = e.target.elements;
        teamCall("/users", { method: "POST", body: JSON.stringify({ username: el.username.value.trim(), password: el.password.value,
          role: el.role.value, projects: projects.filter(p => el[`proj-${p.id}`] && el[`proj-${p.id}`].checked).map(p => p.id) }) }, "new-user");
      } },
      field("User name", h("input", { name: "username", required: true, maxlength: 64, autocomplete: "off", "data-key": "new-user" })),
      field("Password", h("input", { name: "password", type: "password", required: true, minlength: 10, autocomplete: "new-password" }),
        "At least 10 characters; they can change it"),
      field("Role", choice("role", ROLE_OPTIONS, "tester")),
      h("fieldset", {}, h("legend", {}, "Projects"), projects.map(p => h("label", { class: "check" },
        h("input", { type: "checkbox", name: `proj-${p.id}`, checked: p.id === "p_default" }), p.name))),
      h("button", { class: "btn quiet", type: "submit" }, "Add user")));
}

function projectsSection(projects) {
  return h("div", { class: "section" }, h("h2", {}, "Projects"),
    h("p", { class: "intro" }, "Each project keeps its own scripts, scenarios, runs and schedules; people see only their projects. Add people to projects in the Users table."),
    h("div", { class: "tablewrap" }, h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["Name", "Members", "Items", ""].map(t => h("th", { scope: "col" }, t)))),
      h("tbody", {}, projects.map(p => h("tr", {},
        h("td", {}, h("input", { value: p.name, maxlength: 60, "aria-label": `Name of project ${p.name}`, "data-key": `pname-${p.id}`,
          onchange: (e) => teamCall(`/projects/${p.id}`, { method: "PUT",
            body: JSON.stringify({ name: e.target.value.trim(), members: p.members.map(m => m.id) }) }, `pname-${p.id}`) })),
        h("td", {}, p.members.length ? p.members.map(m => m.username).join(", ") : h("span", { class: "empty" }, "administrators only")),
        h("td", { class: "num" }, fmt(p.usage)),
        h("td", { class: "rowactions" }, p.id === "p_default" ? null : h("button", { class: "btn quiet small", type: "button",
          disabled: p.usage > 0, title: p.usage > 0 ? "Delete its scripts, scenarios, runs and schedules first" : null,
          "aria-label": `Delete project ${p.name}`,
          onclick: () => confirm(`Delete the project ${p.name}?`) && teamCall(`/projects/${p.id}`, { method: "DELETE" }) }, "Delete"))))))),
    h("form", { class: "inline", "aria-label": "Add a project", onsubmit: (e) => {
        e.preventDefault();
        teamCall("/projects", { method: "POST", body: JSON.stringify({ name: e.target.elements.name.value.trim() }) }, "new-project");
      } },
      field("New project", h("input", { name: "name", required: true, maxlength: 60, "data-key": "new-project" })),
      h("button", { class: "btn quiet", type: "submit" }, "Add project")));
}

function renderTeam(focusKey) {
  if (!state.team) {              // open access and no accounts yet
    setMain(h("h1", {}, "Team"),
      h("p", { class: "intro" }, "Right now anyone who can reach this server can use it, with every right. Create an administrator account to require sign-in."),
      h("div", { class: "formerror", id: "teamerror", role: "alert" }),
      setupForm());
    restoreFocus(focusKey || "setup-user");
    return;
  }
  const { users, projects } = state.team;
  setMain(
    h("h1", {}, "Team"),
    h("div", { class: "formerror", id: "teamerror", role: "alert" }),
    state.me.setup_needed ? setupForm() : null,
    usersSection(users, projects),
    projectsSection(projects));
  restoreFocus(focusKey);
}

// ---- your account: password and API tokens -----------------------------------------------------

async function openAccount() {
  clearTimeout(state.timer);
  clearCharts();
  const me = state.me;
  if (!me.user_id) {
    setMain(h("h1", {}, "Your account"), h("div", { class: "notice" }, me.mode === "open"
      ? "There are no accounts yet. Create the first administrator on the Team page."
      : "You are using the server's API key, which has no account. Sign in with a user account to manage a password and API tokens."));
    return;
  }
  try { state.tokens = await api("/auth/tokens"); } catch (e) { setMain(h("div", { class: "notice" }, e.message)); return; }
  renderAccount();
}

function accountError(text) { const el = $("#accounterror"); if (el) el.textContent = text; }

async function changePassword(e) {
  e.preventDefault();
  const el = e.target.elements;
  accountError("");
  if (el.next.value !== el.again.value) { accountError("The two new passwords differ."); return; }
  try {
    await api("/auth/password", { method: "POST",
      body: JSON.stringify({ current_password: el.current.value, new_password: el.next.value }) });
  } catch (ex) { accountError(ex.message); return; }
  e.target.reset();
  const done = $("#pwstate");
  if (done) done.textContent = "Password changed. Your other sessions were signed out.";
}

function tokensTable() {
  if (!state.tokens.length) return h("p", { class: "empty" }, "No tokens.");
  return h("div", { class: "tablewrap" }, h("table", { class: "grid" },
    h("thead", {}, h("tr", {}, ["Name", "Created", "Last used", ""].map(t => h("th", { scope: "col" }, t)))),
    h("tbody", {}, state.tokens.map(t => h("tr", {},
      h("td", {}, t.name), h("td", {}, ago(t.created_at)), h("td", {}, t.last_used_at ? ago(t.last_used_at) : "never"),
      h("td", { class: "rowactions" }, h("button", { class: "btn quiet small", type: "button", "aria-label": `Revoke ${t.name}`,
        onclick: async () => {
          if (!confirm(`Revoke the token ${t.name}? Anything using it stops working.`)) return;
          try { await api(`/auth/tokens/${t.id}`, { method: "DELETE" }); state.tokens = await api("/auth/tokens"); renderAccount(); }
          catch (ex) { accountError(ex.message); }
        } }, "Revoke")))))));
}

function renderAccount(created, focusKey) {
  const me = state.me;
  setMain(
    h("h1", {}, "Your account"),
    h("p", { class: "verdict" }, `${me.name} · ${ROLE_NAMES[me.role]}`),
    h("div", { class: "formerror", id: "accounterror", role: "alert" }),
    h("div", { class: "section" }, h("h2", {}, "Password"),
      h("form", { class: "inline", "aria-label": "Change your password", onsubmit: changePassword },
        field("Current password", h("input", { name: "current", type: "password", required: true, autocomplete: "current-password" })),
        field("New password", h("input", { name: "next", type: "password", required: true, minlength: 10, autocomplete: "new-password" })),
        field("Repeat the new password", h("input", { name: "again", type: "password", required: true, autocomplete: "new-password" })),
        h("button", { class: "btn quiet", type: "submit" }, "Change password")),
      h("p", { class: "savestate", id: "pwstate", role: "status" })),
    h("div", { class: "section" }, h("h2", {}, "API tokens"),
      h("p", { class: "intro" }, "For the command line and pipelines: send a token in the X-API-Key header, or set PERF_API_KEY for ",
        h("code", {}, "python -m app.cli"), ". A token acts as you, with your role and projects. Revoke it when it is no longer needed."),
      created ? h("div", { class: "notice good", role: "status" },
        h("p", {}, `Your new token "${created.name}". Copy it now: it is not shown again.`), h("code", {}, created.token)) : null,
      tokensTable(),
      h("form", { class: "inline", "aria-label": "Create an API token", onsubmit: async (e) => {
          e.preventDefault();
          accountError("");
          try {
            const made = await api("/auth/tokens", { method: "POST", body: JSON.stringify({ name: e.target.elements.name.value.trim() }) });
            state.tokens = await api("/auth/tokens");
            renderAccount(made, "token-name");
          } catch (ex) { accountError(ex.message); }
        } },
        field("Token name", h("input", { name: "name", required: true, maxlength: 60, placeholder: "ci-pipeline", "data-key": "token-name" })),
        h("button", { class: "btn quiet", type: "submit" }, "Create token"))));
  restoreFocus(focusKey);
}
