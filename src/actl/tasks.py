"""SQLite-backed task storage for actl.

Stage 2 keeps the project's tasks in a small SQLite database at
``.actl/tasks.db``. The schema has three tables: ``tasks``, ``dependencies``,
and ``reviews``. There is no BLOCKED status - whether a task is blocked is
derived from its dependencies when needed. A task's status is PENDING,
RUNNING, IMPLEMENTED, FAILED, APPROVED, or NEEDS_CHANGES (Stage 5); only
``actl task run`` moves a task between them.

Reviewer verdicts are stored in ``reviews`` (one row per completed review,
newest last) so later stages can read the findings that belong to a task's
current implementation.

The module is deliberately small: repository functions over a connection
opened by the caller. Every write runs inside a ``with conn:`` transaction,
so a failed command never leaves a half-written task behind, and foreign
keys are enabled on every connection. Operations that validate state before
writing (update and delete) open their transaction with ``BEGIN IMMEDIATE``:
the write lock is taken before the dependency graph is read, so two
overlapping commands cannot both validate against the same unchanged graph.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DATABASE_NAME = "tasks.db"
STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_IMPLEMENTED = "IMPLEMENTED"
STATUS_FAILED = "FAILED"
STATUS_APPROVED = "APPROVED"
STATUS_NEEDS_CHANGES = "NEEDS_CHANGES"
STATUSES = (
    STATUS_PENDING, STATUS_RUNNING, STATUS_IMPLEMENTED, STATUS_FAILED,
    STATUS_APPROVED, STATUS_NEEDS_CHANGES,
)
REVIEW_APPROVED = "approved"
REVIEW_CHANGES_REQUESTED = "changes_requested"
REVIEW_DECISIONS = (REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED)

# CREATE TABLE IF NOT EXISTS keeps opening the database idempotent: existing
# tasks are preserved while a missing or older database gains the current
# schema. ON DELETE RESTRICT is a safety net; delete_task() refuses deletions
# with dependents and names them in the error message.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dependencies (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    depends_on_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    PRIMARY KEY (task_id, depends_on_id),
    CHECK (task_id <> depends_on_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    commit_sha TEXT NOT NULL,
    decision TEXT NOT NULL,
    findings TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class TaskError(Exception):
    """Raised when a task operation is invalid or the database fails."""


@dataclass(frozen=True)
class Task:
    """A stored task plus the ids it depends on (sorted ascending)."""

    id: int
    title: str
    description: str
    status: str
    created_at: str
    updated_at: str
    depends_on: tuple


def connect(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path``, creating the schema when needed.

    Raises :class:`TaskError` when the file cannot be opened or is not a
    usable SQLite database; a damaged file is never overwritten.
    """
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        raise TaskError(
            "cannot open task database " + str(db_path) + " (" + str(exc) + ")"
        )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
    except sqlite3.Error as exc:
        conn.close()
        raise TaskError(
            "cannot open task database " + str(db_path) + " (" + str(exc) + ")"
        )
    return conn


def create_task(conn, title, description, depends_on=()) -> Task:
    """Insert a new PENDING task and return it."""
    title = title.strip()
    if not title:
        raise TaskError("task title must not be empty")
    now = _utc_now()
    with conn:
        cursor = conn.execute(
            "INSERT INTO tasks (title, description, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (title, description, STATUS_PENDING, now, now),
        )
        task_id = cursor.lastrowid
        _set_dependencies(
            conn, task_id, _resolve_dependencies(conn, task_id, depends_on)
        )
    return get_task(conn, task_id)


def list_tasks(conn) -> list:
    """Return every task, ordered by id."""
    rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    return [_to_task(row, _dependencies(conn, row["id"])) for row in rows]


def get_task(conn, task_id: int) -> Task:
    """Return one task or raise :class:`TaskError` when it does not exist."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise TaskError("task t" + str(task_id) + " does not exist")
    return _to_task(row, _dependencies(conn, task_id))


def update_task(conn, task_id: int, title=None, description=None, depends_on=None) -> Task:
    """Update the given fields of ``task_id`` and return the task.

    ``title`` and ``description`` are changed when not None. ``depends_on``
    replaces the dependency set when not None (an empty sequence clears it).

    Reading and validating the dependency graph happens inside the write
    transaction: ``BEGIN IMMEDIATE`` takes the lock before the graph is read,
    so two overlapping updates cannot both validate against the same
    unchanged graph and then commit a dependency cycle.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _task_exists(conn, task_id):
            raise TaskError("task t" + str(task_id) + " does not exist")
        changes = {}
        if title is not None:
            title = title.strip()
            if not title:
                raise TaskError("task title must not be empty")
            changes["title"] = title
        if description is not None:
            changes["description"] = description
        resolved = None
        if depends_on is not None:
            resolved = _resolve_dependencies(conn, task_id, depends_on)
        if not changes and resolved is None:
            raise TaskError(
                "no changes requested for task t" + str(task_id)
                + " (use --title, --description, --description-file, --depends-on,"
                " or --clear-dependencies)"
            )
        if resolved is not None:
            _set_dependencies(conn, task_id, resolved)
        changes["updated_at"] = _utc_now()
        # Column names come from the fixed set above, never from user input.
        assignments = ", ".join(name + " = ?" for name in changes)
        conn.execute(
            "UPDATE tasks SET " + assignments + " WHERE id = ?",
            (*changes.values(), task_id),
        )
    return get_task(conn, task_id)


def set_task_status(conn, task_id: int, status: str) -> Task:
    """Set the task's status and return the task.

    Only the statuses actl defines are accepted; the caller decides which
    transitions are legal (``actl task run`` moves tasks between them).
    """
    if status not in STATUSES:
        raise TaskError("invalid task status " + repr(status))
    with conn:
        cursor = conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, _utc_now(), task_id),
        )
        if cursor.rowcount == 0:
            raise TaskError("task t" + str(task_id) + " does not exist")
    return get_task(conn, task_id)


def record_review(conn, task_id: int, commit_sha: str, decision: str, findings) -> None:
    """Store one reviewer verdict for later stages.

    ``findings`` is a list of plain JSON-safe dicts; it is stored as JSON so
    Stage 6 can feed the findings back to a repair worker without reopening
    the reviewer's transcript.
    """
    if decision not in REVIEW_DECISIONS:
        raise TaskError("invalid review decision " + repr(decision))
    if not isinstance(findings, list):
        raise TaskError("review findings must be a list")
    with conn:
        if not _task_exists(conn, task_id):
            raise TaskError("task t" + str(task_id) + " does not exist")
        conn.execute(
            "INSERT INTO reviews (task_id, commit_sha, decision, findings, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (task_id, commit_sha, decision, json.dumps(findings), _utc_now()),
        )


def latest_review(conn, task_id: int):
    """Return the newest stored review for ``task_id``, or None."""
    row = conn.execute(
        "SELECT commit_sha, decision, findings, created_at FROM reviews"
        " WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "commit_sha": row["commit_sha"],
        "decision": row["decision"],
        "findings": json.loads(row["findings"]),
        "created_at": row["created_at"],
    }


def delete_task(conn, task_id: int) -> None:
    """Delete ``task_id`` unless other tasks depend on it.

    The dependent check runs inside the same immediate transaction as the
    deletion, so a concurrent dependency update cannot slip in between the
    check and the write.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _task_exists(conn, task_id):
            raise TaskError("task t" + str(task_id) + " does not exist")
        dependents = [
            row["task_id"]
            for row in conn.execute(
                "SELECT task_id FROM dependencies WHERE depends_on_id = ? ORDER BY task_id",
                (task_id,),
            )
        ]
        if dependents:
            names = ", ".join("t" + str(dependent) for dependent in dependents)
            raise TaskError(
                "cannot delete task t" + str(task_id) + ": " + names
                + (" depend on it" if len(dependents) > 1 else " depends on it")
            )
        conn.execute("DELETE FROM dependencies WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM reviews WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def _resolve_dependencies(conn, task_id: int, depends_on) -> list:
    """Validate dependency ids for ``task_id``; return them deduplicated."""
    resolved = []
    for dep in depends_on:
        if dep == task_id:
            raise TaskError("task t" + str(task_id) + " cannot depend on itself")
        if dep not in resolved:
            resolved.append(dep)
    for dep in resolved:
        if not _task_exists(conn, dep):
            raise TaskError(
                "cannot add dependency t" + str(dep) + " to t" + str(task_id)
                + ": task t" + str(dep) + " does not exist"
            )
        if _would_create_cycle(conn, task_id, dep):
            raise TaskError(
                "cannot add dependency t" + str(dep) + " to t" + str(task_id)
                + ": it would create a dependency cycle"
            )
    return resolved


def _set_dependencies(conn, task_id: int, depends_on) -> None:
    conn.execute("DELETE FROM dependencies WHERE task_id = ?", (task_id,))
    conn.executemany(
        "INSERT INTO dependencies (task_id, depends_on_id) VALUES (?, ?)",
        [(task_id, dep) for dep in depends_on],
    )


def _dependencies(conn, task_id: int) -> list:
    return [
        row["depends_on_id"]
        for row in conn.execute(
            "SELECT depends_on_id FROM dependencies WHERE task_id = ?"
            " ORDER BY depends_on_id",
            (task_id,),
        )
    ]


def _task_exists(conn, task_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        is not None
    )


def _would_create_cycle(conn, task_id: int, depends_on_id: int) -> bool:
    """Return True when ``task_id`` is reachable from ``depends_on_id``.

    Following ``depends_on`` edges from a candidate dependency must never
    lead back to the task, because that would close a dependency cycle.
    UNION (not UNION ALL) also terminates on already-corrupt data.
    """
    row = conn.execute(
        """
        WITH RECURSIVE reachable(id) AS (
            SELECT ?
            UNION
            SELECT dependencies.depends_on_id
            FROM dependencies
            JOIN reachable ON dependencies.task_id = reachable.id
        )
        SELECT 1 FROM reachable WHERE id = ? LIMIT 1
        """,
        (depends_on_id, task_id),
    ).fetchone()
    return row is not None


def _to_task(row, depends_on) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        depends_on=tuple(depends_on),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
