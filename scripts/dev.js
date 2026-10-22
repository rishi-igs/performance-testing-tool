#!/usr/bin/env node
// Start the sample target (port 9000) and the tool (port 8000) for local development: npm run dev
// npm start runs the tool only. The tool also serves the dashboard, so there is no separate frontend server.
// Requires JMeter on PATH, or set JMETER_BIN to the full path of bin/jmeter (jmeter.bat on Windows).
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const backend = path.join(__dirname, "..", "backend");
const windows = process.platform === "win32";
const venv = path.join(backend, ".venv", windows ? "Scripts/python.exe" : "bin/python");
const python = process.env.PYTHON || (fs.existsSync(venv) ? venv : windows ? "python" : "python3");
const env = { ...process.env, ALLOW_PRIVATE_TARGETS: process.env.ALLOW_PRIVATE_TARGETS || "true" }; // local testing only
const withSample = !process.argv.includes("--tool-only");

function server(name, args) {
  const child = spawn(python, ["-m", "uvicorn", ...args], { cwd: backend, env, stdio: "inherit" });
  child.on("error", (err) => {
    console.error(`Could not start the ${name} with ${python}: ${err.message}. Set PYTHON to your Python executable.`);
    process.exitCode = 1;
  });
  return child;
}

const sample = withSample && server("sample target", ["tests.sample_app:app", "--port", "9000", "--log-level", "warning"]);
const tool = server("tool", ["app.main:app", "--host", "127.0.0.1", "--port", "8000"]);
const running = (child) => child && child.exitCode === null && child.signalCode === null;

console.log(withSample ? "Sample target: http://127.0.0.1:9000   Dashboard: http://127.0.0.1:8000"
                       : "Dashboard: http://127.0.0.1:8000");

// Ctrl+C reaches the servers too, and the tool stops its running tests before it exits, so wait for it.
// A second Ctrl+C stops everything at once.
let interrupted = false;
process.on("SIGINT", () => {
  if (interrupted) process.exit(1);
  interrupted = true;
});
process.on("SIGTERM", () => tool.kill("SIGTERM"));

if (sample) sample.on("exit", (code) => {
  if (!interrupted && running(tool)) console.warn(`The sample target stopped (exit code ${code}).`);
});
tool.on("exit", (code) => {
  if (running(sample)) sample.kill();
  if (code) process.exitCode = code;
});
process.on("exit", () => {
  for (const child of [sample, tool]) if (running(child)) child.kill();
});
