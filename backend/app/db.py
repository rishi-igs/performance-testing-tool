"""Small SQLite store for test metadata and summaries."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tests (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,
    config_json TEXT NOT NULL,
    summary_json TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    exit_code   INTEGER,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS scripts (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    source         TEXT NOT NULL,
    request_count  INTEGER NOT NULL,
    items_json     TEXT NOT NULL,
    variables_json TEXT NOT NULL,
    warnings_json  TEXT NOT NULL,
    rules_json     TEXT NOT NULL,
    overrides_json TEXT NOT NULL,
    original       BLOB,
    created_by     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS script_files (
    script_id TEXT NOT NULL,
    name      TEXT NOT NULL,
    content   BLOB NOT NULL,
    PRIMARY KEY (script_id, name)
);

CREATE TABLE IF NOT EXISTS scenarios (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generators (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status_json TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitors (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status_json TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS schedules (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    config_json TEXT NOT NULL,
    next_run_at TEXT,
    last_run_at TEXT,
    last_run_id TEXT,
    last_error  TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS debug_runs (
    id           TEXT PRIMARY KEY,
    script_id    TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    exit_code    INTEGER,
    error        TEXT,
    summary_json TEXT
);
"""

# Columns added after the first release; existing databases get them on startup.
MIGRATIONS = [
    ("scripts", "design_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("scripts", "suggestions_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("tests", "kind", "TEXT NOT NULL DEFAULT 'quick'"),
    ("tests", "scenario_id", "TEXT"),
    ("tests", "project_id", "TEXT NOT NULL DEFAULT 'p_default'"),
    ("scripts", "project_id", "TEXT NOT NULL DEFAULT 'p_default'"),
    ("scenarios", "project_id", "TEXT NOT NULL DEFAULT 'p_default'"),
    ("scenarios", "baseline_run_id", "TEXT"),
]
DEFAULT_PROJECT = "p_default"

UPDATABLE = {"status", "summary_json", "started_at", "finished_at", "exit_code", "error"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            for table, column, declaration in MIGRATIONS:
                if column not in {row[1] for row in c.execute(f"PRAGMA table_info({table})")}:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            c.execute("INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (?, 'Default', ?)",
                      (DEFAULT_PROJECT, now_iso()))

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def insert_test(self, test_id: str, name: str, config: dict[str, Any], created_by: str, *,
                    kind: str = "quick", scenario_id: str | None = None, project_id: str = DEFAULT_PROJECT) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO tests (id, name, status, config_json, created_by, created_at, kind, scenario_id, project_id) "
                "VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                (test_id, name, json.dumps(config), created_by, now_iso(), kind, scenario_id, project_id),
            )

    def update_test(self, test_id: str, **fields: Any) -> None:
        bad = set(fields) - UPDATABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE tests SET {cols} WHERE id = ?", (*fields.values(), test_id))

    def get_test(self, test_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _projects_clause(projects: set[str] | None, where: list[str], args: list[Any]) -> None:
        """Limit a query to the given projects (None = every project)."""
        if projects is not None:
            where.append(f"project_id IN ({','.join('?' * len(projects)) or 'NULL'})")
            args.extend(sorted(projects))

    def list_tests(self, limit: int = 100, *, kind: str | None = None, scenario_id: str | None = None,
                   projects: set[str] | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if scenario_id:
            where.append("scenario_id = ?")
            args.append(scenario_id)
        self._projects_clause(projects, where, args)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM tests {clause}ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)
            ).fetchall()
        return [self._row(r) for r in rows]

    # ---- load generators and monitors (same shape: a name, a config, a last status) ----------

    _RESOURCE_TABLES = {"generators", "monitors"}

    def insert_resource(self, table: str, item_id: str, config: dict[str, Any]) -> None:
        assert table in self._RESOURCE_TABLES
        with self._conn() as c:
            c.execute(f"INSERT INTO {table} (id, name, config_json, created_at) VALUES (?, ?, ?, ?)",
                      (item_id, config["name"], json.dumps(config), now_iso()))

    def update_resource(self, table: str, item_id: str, config: dict[str, Any] | None = None,
                        status: dict[str, Any] | None = None) -> bool:
        assert table in self._RESOURCE_TABLES
        sets, args = [], []
        if config is not None:
            sets += ["name = ?", "config_json = ?"]
            args += [config["name"], json.dumps(config)]
        if status is not None:
            sets.append("status_json = ?")
            args.append(json.dumps(status))
        with self._conn() as c:
            return c.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", (*args, item_id)).rowcount > 0

    def get_resource(self, table: str, item_id: str) -> dict[str, Any] | None:
        assert table in self._RESOURCE_TABLES
        with self._conn() as c:
            row = c.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
        return self._resource(row) if row else None

    def list_resources(self, table: str) -> list[dict[str, Any]]:
        assert table in self._RESOURCE_TABLES
        with self._conn() as c:
            rows = c.execute(f"SELECT * FROM {table} ORDER BY name").fetchall()
        return [self._resource(r) for r in rows]

    def delete_resource(self, table: str, item_id: str) -> bool:
        assert table in self._RESOURCE_TABLES
        with self._conn() as c:
            return c.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,)).rowcount > 0

    @staticmethod
    def _resource(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        status = d.pop("status_json")
        d["status"] = json.loads(status) if status else None
        return d

    # ---- scenarios ------------------------------------------------------------------------

    def insert_scenario(self, scenario_id: str, config: dict[str, Any], created_by: str,
                        project_id: str = DEFAULT_PROJECT) -> None:
        now = now_iso()
        with self._conn() as c:
            c.execute("INSERT INTO scenarios (id, name, config_json, created_by, created_at, updated_at, project_id) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (scenario_id, config["name"], json.dumps(config), created_by, now, now, project_id))

    def set_baseline(self, scenario_id: str, run_id: str | None) -> None:
        with self._conn() as c:
            c.execute("UPDATE scenarios SET baseline_run_id = ? WHERE id = ?", (run_id, scenario_id))

    def update_scenario(self, scenario_id: str, config: dict[str, Any]) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE scenarios SET name = ?, config_json = ?, updated_at = ? WHERE id = ?",
                             (config["name"], json.dumps(config), now_iso(), scenario_id)).rowcount > 0

    def get_scenario(self, scenario_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM scenarios WHERE id = ?", (scenario_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        return d

    def list_scenarios(self, limit: int = 200, projects: set[str] | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        self._projects_clause(projects, where, args)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        with self._conn() as c:
            rows = c.execute("SELECT id, name, config_json, created_by, created_at, updated_at, project_id, baseline_run_id "
                             f"FROM scenarios {clause}ORDER BY updated_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            cfg = json.loads(d.pop("config_json"))
            d.update(mode=cfg.get("mode"), groups=len(cfg.get("groups", [])),
                     scripts=sorted({g["script_id"] for g in cfg.get("groups", [])}))
            out.append(d)
        return out

    def delete_scenario(self, scenario_id: str) -> bool:
        """Delete a scenario and its schedules; its runs are kept."""
        with self._conn() as c:
            c.execute("DELETE FROM schedules WHERE scenario_id = ?", (scenario_id,))
            return c.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,)).rowcount > 0

    def mark_interrupted(self) -> int:
        """Tests left 'running' by a previous process can no longer be tracked."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE tests SET status = 'failed', finished_at = ?, "
                "error = 'Server restarted while the test was running' WHERE status = 'running'",
                (now_iso(),),
            )
            return cur.rowcount

    # ---- imported scripts -------------------------------------------------------------

    def insert_script(self, script_id: str, *, name: str, source: str, items: list[dict[str, Any]],
                      variables: list[str], warnings: list[str], rules: dict[str, Any],
                      original: bytes | None, created_by: str, design: dict[str, Any] | None = None,
                      suggestions: dict[str, Any] | None = None, project_id: str = DEFAULT_PROJECT) -> None:
        now = now_iso()
        with self._conn() as c:
            c.execute(
                "INSERT INTO scripts (id, name, source, request_count, items_json, variables_json, warnings_json, "
                "rules_json, overrides_json, original, created_by, created_at, updated_at, design_json, "
                "suggestions_json, project_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?)",
                (script_id, name, source, len(items), json.dumps(items), json.dumps(variables),
                 json.dumps(warnings), json.dumps(rules), original, created_by, now, now,
                 json.dumps(design or {}), json.dumps(suggestions or {}), project_id),
            )

    def get_script(self, script_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ("items", "variables", "warnings", "rules", "overrides", "design", "suggestions"):
            d[key] = json.loads(d.pop(f"{key}_json"))
        return d

    def update_design(self, script_id: str, mutate: Callable[[dict[str, Any]], dict[str, Any]]) -> bool:
        """Read the design, apply `mutate`, write it back, in one transaction."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT design_json FROM scripts WHERE id = ?", (script_id,)).fetchone()
            if not row:
                return False
            c.execute("UPDATE scripts SET design_json = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(mutate(json.loads(row["design_json"]))), now_iso(), script_id))
        return True

    def update_suggestions(self, script_id: str, mutate: Callable[[dict[str, Any]], dict[str, Any]]) -> bool:
        """Read the suggestions, apply `mutate`, write them back, in one transaction."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT suggestions_json FROM scripts WHERE id = ?", (script_id,)).fetchone()
            if not row:
                return False
            c.execute("UPDATE scripts SET suggestions_json = ? WHERE id = ?",
                      (json.dumps(mutate(json.loads(row["suggestions_json"] or "{}"))), script_id))
        return True

    def put_script_file(self, script_id: str, name: str, content: bytes) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO script_files (script_id, name, content) VALUES (?, ?, ?)",
                      (script_id, name, content))

    def script_files(self, script_id: str) -> dict[str, bytes]:
        with self._conn() as c:
            rows = c.execute("SELECT name, content FROM script_files WHERE script_id = ?", (script_id,)).fetchall()
        return {r["name"]: bytes(r["content"]) for r in rows}

    def delete_script_file(self, script_id: str, name: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM script_files WHERE script_id = ? AND name = ?",
                             (script_id, name)).rowcount > 0

    # ---- debug runs (one user, one iteration, full request/response capture) --------------

    def insert_debug_run(self, run_id: str, script_id: str, created_by: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO debug_runs (id, script_id, status, created_by, created_at) "
                      "VALUES (?, ?, 'running', ?, ?)", (run_id, script_id, created_by, now_iso()))

    def update_debug_run(self, run_id: str, **fields: Any) -> None:
        bad = set(fields) - UPDATABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            with self._conn() as c:
                c.execute(f"UPDATE debug_runs SET {cols} WHERE id = ?", (*fields.values(), run_id))

    def get_debug_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM debug_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        summary = d.pop("summary_json")
        d["summary"] = json.loads(summary) if summary else None
        return d

    def list_debug_runs(self, script_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT id FROM debug_runs WHERE script_id = ? ORDER BY created_at DESC, rowid DESC "
                             "LIMIT ?", (script_id, limit)).fetchall()
        return [self.get_debug_run(r["id"]) for r in rows]

    def prune_debug_runs(self, script_id: str, keep: int) -> list[str]:
        """Delete all but the newest `keep` finished debug runs of a script; return the deleted ids."""
        with self._conn() as c:
            rows = c.execute("SELECT id FROM debug_runs WHERE script_id = ? AND status != 'running' "
                             "ORDER BY created_at DESC, rowid DESC", (script_id,)).fetchall()
            old = [r["id"] for r in rows[keep:]]
            c.executemany("DELETE FROM debug_runs WHERE id = ?", [(i,) for i in old])
        return old

    def mark_debug_runs_interrupted(self) -> int:
        with self._conn() as c:
            return c.execute("UPDATE debug_runs SET status = 'failed', finished_at = ?, "
                             "error = 'Server restarted during the debug run' WHERE status = 'running'",
                             (now_iso(),)).rowcount

    def list_scripts(self, limit: int = 100, projects: set[str] | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        self._projects_clause(projects, where, args)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, name, source, request_count, created_by, created_at, updated_at, project_id FROM scripts "
                f"{clause}ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- users, sessions, API tokens ---------------------------------------------------------

    def count_users(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, user_id: str, username: str, password_hash: str, role: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                      (user_id, username, password_hash, role, now_iso()))

    def create_first_user(self, user_id: str, username: str, password_hash: str) -> bool:
        """Create the first administrator, only while there are no accounts at all (False if one exists)."""
        with self._conn() as c:
            return c.execute("INSERT INTO users (id, username, password_hash, role, created_at) "
                             "SELECT ?, ?, ?, 'admin', ? WHERE NOT EXISTS (SELECT 1 FROM users)",
                             (user_id, username, password_hash, now_iso())).rowcount > 0

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT id, username, role, disabled, created_at, last_login_at FROM users ORDER BY username").fetchall()
        return [dict(r) for r in rows]

    def update_user(self, user_id: str, **fields: Any) -> None:
        allowed = {"role", "disabled", "password_hash", "last_login_at"}
        if set(fields) - allowed:
            raise ValueError(f"cannot update fields: {sorted(set(fields) - allowed)}")
        if fields:
            with self._conn() as c:
                c.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), user_id))

    def delete_user(self, user_id: str) -> bool:
        with self._conn() as c:
            gone = c.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount > 0
            for table in ("sessions", "api_tokens", "project_members"):
                c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        return gone

    def create_session(self, token_hash: str, user_id: str, expires_at: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
            c.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                      (token_hash, user_id, now_iso(), expires_at))

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE token_hash = ? AND expires_at > ?", (token_hash, now_iso())).fetchone()
        return dict(row) if row else None

    def delete_session(self, token_hash: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, user_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def create_token(self, token_id: str, user_id: str, name: str, token_hash: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO api_tokens (id, user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                      (token_id, user_id, name, token_hash, now_iso()))

    def get_token_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            if row:
                c.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now_iso(), row["id"]))
        return dict(row) if row else None

    def list_tokens(self, user_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT id, name, created_at, last_used_at FROM api_tokens WHERE user_id = ? ORDER BY created_at",
                             (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_token(self, token_id: str, user_id: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, user_id)).rowcount > 0

    # ---- projects -----------------------------------------------------------------------------

    def create_project(self, project_id: str, name: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)", (project_id, name, now_iso()))

    def list_projects(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM projects ORDER BY name").fetchall()
            members = c.execute("SELECT project_id, user_id FROM project_members").fetchall()
        out = [dict(r) | {"members": []} for r in rows]
        by_id = {p["id"]: p for p in out}
        for m in members:
            if m["project_id"] in by_id:
                by_id[m["project_id"]]["members"].append(m["user_id"])
        return out

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return next((p for p in self.list_projects() if p["id"] == project_id), None)

    def update_project(self, project_id: str, name: str, members: list[str]) -> None:
        with self._conn() as c:
            c.execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))
            c.execute("DELETE FROM project_members WHERE project_id = ?", (project_id,))
            c.executemany("INSERT INTO project_members (project_id, user_id) VALUES (?, ?)",
                          [(project_id, u) for u in dict.fromkeys(members)])

    def project_usage(self, project_id: str) -> int:
        with self._conn() as c:
            return sum(c.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id = ?", (project_id,)).fetchone()[0]
                       for t in ("scripts", "scenarios", "tests", "schedules"))

    def delete_project(self, project_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            c.execute("DELETE FROM project_members WHERE project_id = ?", (project_id,))

    def user_projects(self, user_id: str) -> set[str]:
        with self._conn() as c:
            return {r[0] for r in c.execute("SELECT project_id FROM project_members WHERE user_id = ?", (user_id,))}

    def set_user_projects(self, user_id: str, project_ids: list[str]) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM project_members WHERE user_id = ?", (user_id,))
            c.executemany("INSERT INTO project_members (project_id, user_id) VALUES (?, ?)",
                          [(p, user_id) for p in dict.fromkeys(project_ids)])

    def count_admins(self, *, excluding: str | None = None) -> int:
        """Enabled administrators, optionally leaving one user out (to keep at least one)."""
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
                             (excluding or "",)).fetchone()[0]

    # ---- schedules ----------------------------------------------------------------------------

    def insert_schedule(self, schedule_id: str, project_id: str, scenario_id: str, config: dict[str, Any],
                        next_run_at: str | None, created_by: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO schedules (id, project_id, scenario_id, config_json, next_run_at, created_by, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (schedule_id, project_id, scenario_id, json.dumps(config), next_run_at, created_by, now_iso()))

    def update_schedule(self, schedule_id: str, **fields: Any) -> None:
        allowed = {"config_json", "next_run_at", "last_run_at", "last_run_id", "last_error"}
        if set(fields) - allowed:
            raise ValueError(f"cannot update fields: {sorted(set(fields) - allowed)}")
        if fields:
            with self._conn() as c:
                c.execute(f"UPDATE schedules SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                          (*fields.values(), schedule_id))

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return self._schedule(row) if row else None

    def list_schedules(self, *, projects: set[str] | None = None, scenario_id: str | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        if scenario_id:
            where.append("scenario_id = ?")
            args.append(scenario_id)
        self._projects_clause(projects, where, args)
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        with self._conn() as c:
            rows = c.execute(f"SELECT * FROM schedules {clause}ORDER BY next_run_at", args).fetchall()
        return [self._schedule(r) for r in rows]

    def due_schedules(self, now: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM schedules WHERE next_run_at IS NOT NULL AND next_run_at <= ?", (now,)).fetchall()
        return [self._schedule(r) for r in rows]

    def claim_schedule(self, schedule_id: str, due_at: str, next_run_at: str | None, now: str) -> bool:
        """Move a due schedule on to its next time; only one caller wins, so a run never starts twice."""
        with self._conn() as c:
            return c.execute("UPDATE schedules SET next_run_at = ?, last_run_at = ? WHERE id = ? AND next_run_at = ?",
                             (next_run_at, now, schedule_id, due_at)).rowcount > 0

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,)).rowcount > 0

    @staticmethod
    def _schedule(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        return d

    def update_script(
        self, script_id: str,
        mutate: Callable[[dict[str, Any], dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
    ) -> bool:
        """Read rules and edits, apply `mutate`, write them back, all in one transaction."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT rules_json, overrides_json FROM scripts WHERE id = ?", (script_id,)).fetchone()
            if not row:
                return False
            rules, overrides = mutate(json.loads(row["rules_json"]), json.loads(row["overrides_json"]))
            c.execute(
                "UPDATE scripts SET rules_json = ?, overrides_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(rules), json.dumps(overrides), now_iso(), script_id),
            )
        return True

    def delete_script(self, script_id: str) -> list[str] | None:
        """Delete a script with its data files and debug runs; return the debug run ids (None if missing)."""
        with self._conn() as c:
            runs = [r["id"] for r in c.execute("SELECT id FROM debug_runs WHERE script_id = ?", (script_id,))]
            if not c.execute("DELETE FROM scripts WHERE id = ?", (script_id,)).rowcount:
                return None
            c.execute("DELETE FROM script_files WHERE script_id = ?", (script_id,))
            c.execute("DELETE FROM debug_runs WHERE script_id = ?", (script_id,))
        return runs

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        summary = d.pop("summary_json")
        d["summary"] = json.loads(summary) if summary else None
        return d
