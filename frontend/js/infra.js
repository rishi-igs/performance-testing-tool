"use strict";
// Infrastructure: load generators (remote JMeter engines) and server monitors.

async function loadInfra() {
  const [generators, monitors] = await Promise.all([api("/generators"), api("/monitors")]);
  state.infra = { generators, monitors };
  return state.infra;
}

async function openInfra() {
  clearTimeout(state.timer);
  try { await loadInfra(); } catch (e) { setMain(h("div", { class: "notice" }, e.message)); return; }
  renderInfra();
}

function infraError(text) { const el = $("#infraerror"); if (el) el.textContent = text; }

async function infraCall(path, opts, done) {
  infraError("");
  try { await api(path, opts); await loadInfra(); renderInfra(done); }
  catch (e) { infraError(e.message); }
}

function statusCell(status, render) {
  if (!status) return h("span", { class: "empty" }, "not checked");
  const when = status.checked_at ? ` · ${ago(new Date(status.checked_at * 1000).toISOString())}` : "";
  return [h("span", { class: `badge ${status.ok ? "completed" : "failed"}` }, status.ok ? "reachable" : "problem"),
    h("small", { class: "src" }, (status.ok ? render(status) : status.error) + when)];
}

function renderInfra(focusKey) {
  const { generators, monitors } = state.infra;
  clearCharts();
  setMain(
    h("h1", {}, "Load generators and monitors"),
    h("div", { class: "formerror", id: "infraerror", role: "alert" }),

    h("div", { class: "section" }, h("h2", {}, "Load generators"),
      h("p", { class: "intro" }, "Machines running jmeter-server that share a scenario's users (LoadRunner's load generators). Start it on each machine with ",
        h("code", {}, "jmeter-server"), " (JMeter's bin folder). JMeter expects an RMI keystore; for a trusted network start the servers with ",
        h("code", {}, "-Jserver.rmi.ssl.disable=true"), " and set JMETER_RMI_SSL_DISABLE=true here. Pick generators in a scenario to use them."),
      generators.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["Name", "Address", "Status", ""].map(t => h("th", { scope: "col" }, t)))),
        h("tbody", {}, generators.map(g => h("tr", {},
          h("td", {}, g.config.name, g.config.enabled ? null : h("small", { class: "src" }, "disabled")),
          h("td", { class: "mono" }, `${g.config.host}:${g.config.port}`),
          h("td", {}, statusCell(g.status, (s) => `${s.connect_ms} ms to connect`)),
          h("td", { class: "rowactions" },
            can("tester") ? h("button", { class: "btn quiet small", onclick: () => infraCall(`/generators/${g.id}/check`, { method: "POST" }) }, "Check") : null,
            can("admin") ? h("button", { class: "btn quiet small", "aria-label": `Remove ${g.config.name}`,
              onclick: () => confirm(`Remove ${g.config.name}?`) && infraCall(`/generators/${g.id}`, { method: "DELETE" }) }, "Remove") : null))))))
        : h("p", { class: "empty" }, "No load generators: scenarios run on this machine."),
      !can("admin") ? h("p", { class: "empty" }, "Administrators add and remove load generators.") :
      h("form", { class: "inline", onsubmit: (e) => { e.preventDefault(); const el = e.target.elements;
          infraCall("/generators", { method: "POST", body: JSON.stringify({ name: el.name.value.trim(), host: el.host.value.trim(), port: +el.port.value }) }, "gen-name"); } },
        field("Name", h("input", { name: "name", required: true, maxlength: 60, "data-key": "gen-name" })),
        field("Host", h("input", { name: "host", required: true, placeholder: "lg-01.example.local" })),
        field("RMI port", h("input", { name: "port", type: "number", min: 1, max: 65535, value: 1099, class: "short" })),
        h("button", { class: "btn quiet", type: "submit" }, "Add generator"))),

    h("div", { class: "section" }, h("h2", {}, "Server monitors"),
      h("p", { class: "intro" }, "CPU, memory and other metrics read during a run, shown with the response times (LoadRunner's online monitors). On a server, run the agent: ",
        h("a", { href: "/agent/perf_agent.py" }, "download perf_agent.py"), ", then ",
        h("code", {}, "python perf_agent.py --host 0.0.0.0 --port 9101 --token <secret>"),
        ". Or read a Prometheus endpoint such as node_exporter or windows_exporter. Monitor addresses follow the same rules as test targets (ALLOWED_HOSTS)."),
      monitors.length ? h("div", { class: "tablewrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["Name", "Kind", "Address", "Status", ""].map(t => h("th", { scope: "col" }, t)))),
        h("tbody", {}, monitors.map(m => h("tr", {},
          h("td", {}, m.config.name),
          h("td", {}, { agent: "Agent", prometheus: `Prometheus${m.config.preset !== "none" ? ` (${m.config.preset})` : ""}`, local: "This machine" }[m.config.kind]),
          h("td", { class: "mono" }, m.config.url || "–"),
          h("td", {}, statusCell(m.status, (s) => s.values.map(v => `${v.label} ${fmt(v.value, 1)}${v.unit}`).join(", "))),
          h("td", { class: "rowactions" },
            can("tester") ? h("button", { class: "btn quiet small", onclick: () => infraCall(`/monitors/${m.id}/check`, { method: "POST" }) }, "Check") : null,
            can("admin") ? h("button", { class: "btn quiet small", "aria-label": `Remove ${m.config.name}`,
              onclick: () => confirm(`Remove ${m.config.name}?`) && infraCall(`/monitors/${m.id}`, { method: "DELETE" }) }, "Remove") : null))))))
        : h("p", { class: "empty" }, "No monitors yet."),
      !can("admin") ? h("p", { class: "empty" }, "Administrators add and remove monitors.") :
      h("form", { class: "inline", onsubmit: (e) => { e.preventDefault(); const el = e.target.elements;
          const body = { name: el.name.value.trim(), kind: el.kind.value, interval_seconds: +el.interval.value };
          if (body.kind !== "local") body.url = el.url.value.trim();
          if (body.kind === "agent" && el.token.value) body.token = el.token.value;
          if (body.kind === "prometheus") body.preset = el.preset.value;
          infraCall("/monitors", { method: "POST", body: JSON.stringify(body) }, "mon-name"); } },
        field("Name", h("input", { name: "name", required: true, maxlength: 60, "data-key": "mon-name" })),
        field("Kind", choice("kind", [["agent", "Agent"], ["prometheus", "Prometheus"], ["local", "This machine"]], "agent")),
        field("Address", h("input", { name: "url", placeholder: "http://app-01:9101" }), "Not needed for this machine"),
        field("Agent token", h("input", { name: "token", type: "password", autocomplete: "off" })),
        field("Prometheus preset", choice("preset", [["node_exporter", "node_exporter (Linux)"], ["windows_exporter", "windows_exporter"]], "node_exporter")),
        field("Every (s)", h("input", { name: "interval", type: "number", min: 2, max: 60, value: 5, class: "short" })),
        h("button", { class: "btn quiet", type: "submit" }, "Add monitor"))));
  restoreFocus(focusKey);
}

// Generator and monitor picks in the scenario editor.
function infraPicker(d) {
  const infra = state.infra || { generators: [], monitors: [] };
  const toggle = (list, id) => (e) => {
    const i = list.indexOf(id);
    if (e.target.checked && i < 0) list.push(id);
    if (!e.target.checked && i >= 0) list.splice(i, 1);
  };
  return h("div", { class: "section" }, h("h2", {}, "Where it runs and what is watched"),
    h("div", { class: "row" },
      h("fieldset", {}, h("legend", {}, "Load generators"),
        infra.generators.length ? infra.generators.map(g => h("label", { class: "check" },
          h("input", { type: "checkbox", checked: d.generators.includes(g.id), "data-key": `pick-${g.id}`, onchange: toggle(d.generators, g.id) }),
          `${g.config.name} (${g.config.host})`)) : h("p", { class: "empty" }, "None: this machine runs the users. ", h("a", { href: "#/infra" }, "Add one"))),
      h("fieldset", {}, h("legend", {}, "Server monitors"),
        infra.monitors.length ? infra.monitors.map(m => h("label", { class: "check" },
          h("input", { type: "checkbox", checked: d.monitors.includes(m.id), "data-key": `pick-${m.id}`, onchange: toggle(d.monitors, m.id) }),
          m.config.name)) : h("p", { class: "empty" }, "None. ", h("a", { href: "#/infra" }, "Add a monitor")))),
    d.generators.length ? h("p", { class: "empty" }, `Users are split evenly across the ${d.generators.length} generator(s).`) : null);
}
