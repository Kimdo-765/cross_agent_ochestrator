"""SQLite persistence for tasks, iterations and live log lines (stdlib only)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import TaskRun, TaskSpec, TaskStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    data        TEXT NOT NULL          -- TaskRun.to_dict() as JSON
);
CREATE TABLE IF NOT EXISTS iterations (
    task_id     TEXT NOT NULL,
    number      INTEGER NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (task_id, number)
);
CREATE TABLE IF NOT EXISTS logs (
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    at          REAL NOT NULL,
    line        TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
"""


def default_data_dir() -> Path:
    return Path(os.environ.get("CAO_DATA_DIR") or (Path.home() / ".cao"))


class Store:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_data_dir() / "cao.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- tasks ------------------------------------------------------------------

    def save_run(self, run: TaskRun) -> None:
        data = run.to_dict()
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, updated_at, data) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, status=excluded.status, "
                "updated_at=excluded.updated_at, data=excluded.data",
                (run.spec.id, run.spec.title, run.status.value, run.spec.created_at, now, json.dumps(data)),
            )
            for it in run.iterations:
                self._conn.execute(
                    "INSERT INTO iterations (task_id, number, data) VALUES (?,?,?) "
                    "ON CONFLICT(task_id, number) DO UPDATE SET data=excluded.data",
                    (run.spec.id, it.number, json.dumps(it.to_dict())),
                )

    def get_run(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def get_spec(self, task_id: str) -> Optional[TaskSpec]:
        data = self.get_run(task_id)
        return TaskSpec.from_dict(data["spec"]) if data else None

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, status, created_at, updated_at, data FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            data = json.loads(r["data"])
            spec = data.get("spec", {})
            its = data.get("iterations", [])
            out.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "repo_path": spec.get("repo_path"),
                    "worker": spec.get("worker", {}),
                    "reviewer": spec.get("reviewer", {}),
                    "iterations": len(its),
                    "max_iterations": spec.get("loop", {}).get("max_iterations"),
                    "final_score": data.get("final_score"),
                    "last_score": next((i.get("score") for i in reversed(its) if i.get("score") is not None), None),
                    "total_cost_usd": data.get("total_cost_usd", 0.0),
                    "branch": data.get("branch"),
                    "outcome": data.get("outcome", {}),
                }
            )
        return out

    def set_status(self, task_id: str, status: TaskStatus, error: Optional[str] = None) -> None:
        data = self.get_run(task_id)
        if not data:
            return
        data["status"] = status.value
        if error:
            data["error"] = error
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status=?, updated_at=?, data=? WHERE id=?",
                (status.value, time.time(), json.dumps(data), task_id),
            )

    def delete_run(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM iterations WHERE task_id=?", (task_id,))
            self._conn.execute("DELETE FROM logs WHERE task_id=?", (task_id,))
            self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    # -- logs -----------------------------------------------------------------------

    def append_log(self, task_id: str, line: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM logs WHERE task_id=?", (task_id,)).fetchone()
            seq = int(row["m"]) + 1
            self._conn.execute(
                "INSERT INTO logs (task_id, seq, at, line) VALUES (?,?,?,?)", (task_id, seq, time.time(), line)
            )
        return seq

    def clear_logs(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM logs WHERE task_id=?", (task_id,))

    def logs(self, task_id: str, after_seq: int = 0, limit: int = 5000) -> Iterator[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, at, line FROM logs WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                (task_id, after_seq, limit),
            ).fetchall()
        for r in rows:
            yield {"seq": r["seq"], "at": r["at"], "line": r["line"]}
