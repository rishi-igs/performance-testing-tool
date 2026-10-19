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
    correlations_json TEXT NOT NULL DEFAULT '[]',
    parameterization_json TEXT NOT NULL DEFAULT '{}',
    csv_headers_json TEXT NOT NULL DEFAULT '[]',
    csv_row_count INTEGER NOT NULL DEFAULT 0,
    assertions_json TEXT NOT NULL DEFAULT '[]',
    warnings_json  TEXT NOT NULL,
    rules_json     TEXT NOT NULL,
    overrides_json TEXT NOT NULL,
    original       BLOB,
    created_by     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
"""

UPDATABLE = {"status", "summary_json", "started_at", "finished_at", "exit_code", "error"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            columns = {row["name"] for row in c.execute("PRAGMA table_info(scripts)")}
            if "correlations_json" not in columns:
                c.execute("ALTER TABLE scripts ADD COLUMN correlations_json TEXT NOT NULL DEFAULT '[]'")
            for name, declaration in (
                ("parameterization_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("csv_headers_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("csv_row_count", "INTEGER NOT NULL DEFAULT 0"),
                ("assertions_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in columns:
                    c.execute(f"ALTER TABLE scripts ADD COLUMN {name} {declaration}")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS debug_runs (
                    id TEXT PRIMARY KEY,
                    script_id TEXT NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    users INTEGER NOT NULL,
                    loops INTEGER NOT NULL,
                    summary_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS debug_runs_script_created
                    ON debug_runs(script_id, created_at DESC);
            """)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def insert_test(self, test_id: str, name: str, config: dict[str, Any], created_by: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO tests (id, name, status, config_json, created_by, created_at) "
                "VALUES (?, ?, 'running', ?, ?, ?)",
                (test_id, name, json.dumps(config), created_by, now_iso()),
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

    def list_tests(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM tests ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row(r) for r in rows]

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
                      correlations: list[dict[str, Any]] | None = None,
                      assertions: list[dict[str, Any]] | None = None,
                      original: bytes | None, created_by: str) -> None:
        now = now_iso()
        with self._conn() as c:
            c.execute(
                "INSERT INTO scripts (id, name, source, request_count, items_json, variables_json, correlations_json, assertions_json, warnings_json, "
                "rules_json, overrides_json, original, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
                (script_id, name, source, len(items), json.dumps(items), json.dumps(variables),
                 json.dumps(correlations or []), json.dumps(assertions or []), json.dumps(warnings), json.dumps(rules),
                 original, created_by, now, now),
            )

    def get_script(self, script_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ("items", "variables", "warnings", "rules", "overrides", "correlations",
                    "parameterization", "csv_headers", "assertions"):
            column = f"{key}_json"
            d[key] = json.loads(d.pop(column))
        return d

    def update_parameter_csv(self, script_id: str, headers: list[str], row_count: int) -> bool:
        with self._conn() as c:
            return c.execute(
                "UPDATE scripts SET csv_headers_json = ?, csv_row_count = ?, updated_at = ? WHERE id = ?",
                (json.dumps(headers), row_count, now_iso(), script_id),
            ).rowcount > 0

    def update_parameterization(
        self,
        script_id: str,
        parameters: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> bool:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                "UPDATE scripts SET parameterization_json = ?, items_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(parameters), json.dumps(items), now_iso(), script_id),
            )
            return cur.rowcount > 0

    def update_correlations(self, script_id: str, correlations: list[dict[str, Any]]) -> bool:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT correlations_json FROM scripts WHERE id = ?", (script_id,)).fetchone()
            if not row:
                return False
            previous = json.loads(row["correlations_json"])
            variable_renames: dict[str, str] = {}
            for rule in correlations:
                prior = next((old for old in previous
                              if old["source_index"] == rule["source_index"]
                              and set(old["target_indices"]) & set(rule["target_indices"])), None)
                if prior and prior["variable"] != rule["variable"]:
                    variable_renames[prior["variable"]] = rule["variable"]

            items_json = None
            if variable_renames:
                items_row = c.execute("SELECT items_json FROM scripts WHERE id = ?", (script_id,)).fetchone()
                items = json.loads(items_row["items_json"])
                for item in items:
                    for field in ("url", "body"):
                        if isinstance(item.get(field), str):
                            for old_name, new_name in variable_renames.items():
                                item[field] = item[field].replace("${" + old_name + "}", "${" + new_name + "}")
                    updated_headers = []
                    for name, value in item.get("headers", []):
                        for old_name, new_name in variable_renames.items():
                            value = value.replace("${" + old_name + "}", "${" + new_name + "}")
                        updated_headers.append([name, value])
                    item["headers"] = updated_headers
                items_json = json.dumps(items)
            c.execute(
                "UPDATE scripts SET correlations_json = ?, items_json = COALESCE(?, items_json), updated_at = ? WHERE id = ?",
                (json.dumps(correlations), items_json, now_iso(), script_id),
            )
            return True

    def update_assertions(self, script_id: str, assertions: list[dict[str, Any]]) -> bool:
        with self._conn() as c:
            return c.execute(
                "UPDATE scripts SET assertions_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(assertions), now_iso(), script_id),
            ).rowcount > 0

    def insert_debug_run(self, run_id: str, script_id: str, users: int, loops: int) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO debug_runs (id, script_id, status, users, loops, created_at) "
                "VALUES (?, ?, 'running', ?, ?, ?)",
                (run_id, script_id, users, loops, now_iso()),
            )

    def get_debug_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM debug_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json")) if result["summary_json"] else None
        return result

    def list_debug_runs(self, script_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, script_id, status, users, loops, error, created_at, started_at, finished_at "
                "FROM debug_runs WHERE script_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (script_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_debug_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "summary_json", "error", "started_at", "finished_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update debug-run fields: {sorted(bad)}")
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._conn() as c:
            c.execute(f"UPDATE debug_runs SET {columns} WHERE id = ?", (*fields.values(), run_id))

    def mark_interrupted_debug_runs(self) -> int:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE debug_runs SET status = 'failed', finished_at = ?, "
                "error = 'Server restarted while the debug run was running' WHERE status = 'running'",
                (now_iso(),),
            )
            return cur.rowcount

    def list_scripts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, name, source, request_count, created_by, created_at, updated_at FROM scripts "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

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

    def delete_script(self, script_id: str) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM debug_runs WHERE script_id = ?", (script_id,))
            return c.execute("DELETE FROM scripts WHERE id = ?", (script_id,)).rowcount > 0

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        summary = d.pop("summary_json")
        d["summary"] = json.loads(summary) if summary else None
        return d
