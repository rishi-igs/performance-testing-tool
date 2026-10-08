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
                      original: bytes | None, created_by: str) -> None:
        now = now_iso()
        with self._conn() as c:
            c.execute(
                "INSERT INTO scripts (id, name, source, request_count, items_json, variables_json, warnings_json, "
                "rules_json, overrides_json, original, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
                (script_id, name, source, len(items), json.dumps(items), json.dumps(variables),
                 json.dumps(warnings), json.dumps(rules), original, created_by, now, now),
            )

    def get_script(self, script_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ("items", "variables", "warnings", "rules", "overrides"):
            d[key] = json.loads(d.pop(f"{key}_json"))
        return d

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
            return c.execute("DELETE FROM scripts WHERE id = ?", (script_id,)).rowcount > 0

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        summary = d.pop("summary_json")
        d["summary"] = json.loads(summary) if summary else None
        return d
