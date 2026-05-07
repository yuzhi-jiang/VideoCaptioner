"""SQLite task database for the watch command.

Schema
------
tasks
  id          INTEGER PRIMARY KEY AUTOINCREMENT
  file_path   TEXT NOT NULL UNIQUE   -- absolute path to video file
  status      TEXT NOT NULL          -- pending | processing | done | error | skipped
  steps       TEXT NOT NULL          -- JSON list: ["transcribe","optimize","translate","synthesize"]
  priority    INTEGER NOT NULL DEFAULT 50
  added_at    REAL NOT NULL          -- Unix timestamp
  started_at  REAL
  finished_at REAL
  error_msg   TEXT
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

# Task statuses
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT    NOT NULL UNIQUE,
    status      TEXT    NOT NULL DEFAULT 'pending',
    steps       TEXT    NOT NULL DEFAULT '[]',
    priority    INTEGER NOT NULL DEFAULT 50,
    added_at    REAL    NOT NULL,
    started_at  REAL,
    finished_at REAL,
    error_msg   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority DESC, added_at ASC);
"""


def open_db(path: Path) -> sqlite3.Connection:
    """Open (or create) the task database and ensure the schema exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def add_task(conn: sqlite3.Connection, file_path: str, steps: List[str],
             priority: int = 50) -> Optional[int]:
    """Insert a new task.  Returns the new row id, or None if file already tracked."""
    try:
        cur = conn.execute(
            "INSERT INTO tasks (file_path, status, steps, priority, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (file_path, STATUS_PENDING, json.dumps(steps), priority, time.time()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # already in DB


def get_next_task(conn: sqlite3.Connection) -> Optional[Dict]:
    """Fetch the highest-priority pending task and mark it as processing."""
    row = conn.execute(
        "SELECT * FROM tasks WHERE status = ? ORDER BY priority DESC, added_at ASC LIMIT 1",
        (STATUS_PENDING,),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE tasks SET status = ?, started_at = ? WHERE id = ?",
        (STATUS_PROCESSING, time.time(), row["id"]),
    )
    conn.commit()
    return dict(row)


def mark_done(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, finished_at = ? WHERE id = ?",
        (STATUS_DONE, time.time(), task_id),
    )
    conn.commit()


def mark_error(conn: sqlite3.Connection, task_id: int, error_msg: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, finished_at = ?, error_msg = ? WHERE id = ?",
        (STATUS_ERROR, time.time(), error_msg[:2000], task_id),
    )
    conn.commit()


def mark_skipped(conn: sqlite3.Connection, task_id: int, reason: str = "") -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, finished_at = ?, error_msg = ? WHERE id = ?",
        (STATUS_SKIPPED, time.time(), reason, task_id),
    )
    conn.commit()


def reset_task(conn: sqlite3.Connection, file_path: str) -> bool:
    """Reset a task back to pending so it will be reprocessed."""
    cur = conn.execute(
        "UPDATE tasks SET status = ?, started_at = NULL, finished_at = NULL, error_msg = NULL "
        "WHERE file_path = ?",
        (STATUS_PENDING, file_path),
    )
    conn.commit()
    return cur.rowcount > 0


def bump_priority(conn: sqlite3.Connection, file_path: str, priority: int) -> bool:
    """Set priority for a task (higher = processed sooner)."""
    cur = conn.execute(
        "UPDATE tasks SET priority = ? WHERE file_path = ?",
        (priority, file_path),
    )
    conn.commit()
    return cur.rowcount > 0


def get_status_summary(conn: sqlite3.Connection) -> Dict[str, int]:
    """Return count of tasks per status."""
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
    ).fetchall()
    return {row["status"]: row["cnt"] for row in rows}


def get_all_tasks(conn: sqlite3.Connection) -> List[Dict]:
    """Return all tasks ordered by status and priority."""
    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY "
        "CASE status WHEN 'processing' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, "
        "priority DESC, added_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def is_known(conn: sqlite3.Connection, file_path: str) -> bool:
    """Return True if file_path is already in the DB (any status)."""
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE file_path = ?", (file_path,)
    ).fetchone()
    return row is not None
