"""SQLite-backed task storage for josi.

Stage 2 keeps the project's tasks in a small SQLite database at
``.josi/tasks.db``. The schema has five tables: ``tasks``, ``dependencies``,
``reviews``, ``runs``, and ``usage``. There is no BLOCKED status - whether a
task is blocked is derived from its dependencies when needed. A task's status
is PENDING, RUNNING, IMPLEMENTED, FAILED, APPROVED, NEEDS_CHANGES,
NEEDS_HUMAN, STAGED, or DONE; only ``josi task run`` moves a task between
them, except that the explicit ``josi task reset`` intervention returns a
task to PENDING.

Stage 7 groups executions into runs: a run row records the run branch, its
status (RUNNING while executing, COMPLETE once every selected task was staged,
INTEGRATED after ``josi run merge``), the base branch it started from, its
start and expected commits, and its final commit. The expected commit is the
commit the run branch is allowed to point at: the start commit, advanced only
by successfully staged, reviewer-approved tasks. A task that was approved and
fast-forwarded into its run branch is STAGED and keeps the id of that run in
``tasks.run_id``; ``josi run merge`` turns its STAGED tasks DONE once the run
branch is part of the base branch. The ``run_selection`` table records which
tasks each run is responsible for, so a failed batch stays resumable in the
same run and a run is only COMPLETE when its whole selection is staged.

Reviewer verdicts are stored in ``reviews`` (one row per completed review,
newest last) so later stages can read the findings that belong to a task's
current implementation.

Per-invocation model usage is stored in ``usage`` (one row per worker
attempt, review turn, or review-format retry), carrying the task and run it
belongs to, the invocation stage, the model, the token counts, and whatever
cost data OpenCode or the model pricing made available. A missing token or
cost value is stored as SQL NULL, never as zero, so a task's totals can be
summed while incomplete or unavailable amounts stay visible.

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

from .usage import USAGE_AVAILABLE, USAGE_UNAVAILABLE

DATABASE_NAME = "tasks.db"
STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_IMPLEMENTED = "IMPLEMENTED"
STATUS_FAILED = "FAILED"
STATUS_APPROVED = "APPROVED"
STATUS_NEEDS_CHANGES = "NEEDS_CHANGES"
STATUS_NEEDS_HUMAN = "NEEDS_HUMAN"
STATUS_STAGED = "STAGED"
STATUS_DONE = "DONE"
STATUSES = (
    STATUS_PENDING, STATUS_RUNNING, STATUS_IMPLEMENTED, STATUS_FAILED,
    STATUS_APPROVED, STATUS_NEEDS_CHANGES, STATUS_NEEDS_HUMAN, STATUS_STAGED,
    STATUS_DONE,
)
RUN_RUNNING = "RUNNING"
RUN_COMPLETE = "COMPLETE"
RUN_INTEGRATED = "INTEGRATED"
RUN_STATUSES = (RUN_RUNNING, RUN_COMPLETE, RUN_INTEGRATED)
RUN_BRANCH_PREFIX = "josi/run/"
REVIEW_APPROVED = "approved"
REVIEW_CHANGES_REQUESTED = "changes_requested"
REVIEW_DECISIONS = (REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED)

# CREATE TABLE IF NOT EXISTS keeps opening the database idempotent: existing
# tasks are preserved while a missing or older database gains the current
# schema. ON DELETE RESTRICT is a safety net; delete_task() refuses deletions
# with dependents and names them in the error message.
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    status TEXT NOT NULL,
    start_commit TEXT NOT NULL,
    expected_commit TEXT,
    final_commit TEXT,
    partially_integrated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    run_id INTEGER REFERENCES runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_selection (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (run_id, task_id)
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

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    run_id INTEGER REFERENCES runs(id),
    stage TEXT NOT NULL,
    attempt INTEGER,
    model TEXT,
    session_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_total REAL,
    cost_input REAL,
    cost_output REAL,
    cost_source TEXT,
    usage_status TEXT NOT NULL,
    cost_complete INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


class TaskError(Exception):
    """Raised when a task operation is invalid or the database fails."""


@dataclass(frozen=True)
class Task:
    """A stored task plus the ids it depends on (sorted ascending).

    ``run_id`` is the run a STAGED task is staged in; it stays recorded after
    ``josi run merge`` turns the task DONE so the association between a run
    and its tasks survives integration.
    """

    id: int
    title: str
    description: str
    status: str
    created_at: str
    updated_at: str
    depends_on: tuple
    run_id: int = None


@dataclass(frozen=True)
class Run:
    """A run: its branch, lifecycle status, and recorded commits.

    ``status`` is RUNNING while the run executes (an interrupted or stopped
    execution stays RUNNING until it is resumed), COMPLETE once every
    selected task was staged and the final commit was recorded, and
    INTEGRATED after ``josi run merge`` integrated the recorded commit into
    the configured base branch.

    ``expected_commit`` is the only commit the run branch may point at: the
    start commit, advanced atomically by every successful staging. A run
    branch found anywhere else is refused instead of trusted. The recorded
    final commit is always this verified value - never whatever the branch
    happens to point at when execution stops.

    ``partially_integrated`` records explicit partial integration permanently,
    independently of later changes to the selected tasks.
    """

    id: int
    branch: str
    base_branch: str
    status: str
    start_commit: str
    final_commit: object
    expected_commit: object
    created_at: str
    updated_at: str
    partially_integrated: bool = False


@dataclass(frozen=True)
class UsageRecord:
    """One invocation's recorded usage, with its task/run attribution.

    Token and cost fields are ``None`` when the amount was unavailable; the
    value is never silently stored as zero. ``usage_status`` is
    ``"available"`` or ``"unavailable"`` and ``cost_complete`` says whether
    total, input, and output costs are all known.
    """

    id: int
    task_id: int
    run_id: object
    stage: str
    attempt: object
    model: object
    session_id: object
    input_tokens: object
    output_tokens: object
    reasoning_tokens: object
    cache_read_tokens: object
    cache_write_tokens: object
    cost_total: object
    cost_input: object
    cost_output: object
    cost_source: object
    usage_status: str
    cost_complete: bool
    created_at: str


def connect(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path``, creating the schema when needed.

    Raises :class:`TaskError` when the file cannot be opened or is not a
    usable SQLite database; a damaged file is never overwritten. Existing
    databases from earlier stages gain the ``runs`` table and the
    ``tasks.run_id`` column without touching their data.
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
        _migrate(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise TaskError(
            "cannot open task database " + str(db_path) + " (" + str(exc) + ")"
        )
    return conn


def _migrate(conn) -> None:
    """Bring databases from earlier stages up to the current schema.

    The data changes run in one transaction: a connection handed back to a
    caller must never carry an implicit transaction that would make its own
    ``BEGIN IMMEDIATE`` fail.
    """
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
    }
    run_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(runs)")
    }
    with conn:
        if "run_id" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN run_id INTEGER REFERENCES runs(id)"
            )
        if "partially_integrated" not in run_columns:
            conn.execute(
                "ALTER TABLE runs ADD COLUMN partially_integrated INTEGER"
                " NOT NULL DEFAULT 0"
            )
        if "expected_commit" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN expected_commit TEXT")
            conn.execute(
                "UPDATE runs SET expected_commit = final_commit"
                " WHERE final_commit IS NOT NULL"
            )
        # Runs without recorded selection rows (created before this table
        # existed) adopt their staged tasks, so completion checks and ``run
        # show`` see them.
        conn.execute(
            "INSERT OR IGNORE INTO run_selection (run_id, task_id)"
            " SELECT run_id, id FROM tasks WHERE run_id IS NOT NULL"
        )


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

    Only the statuses josi defines are accepted; the caller decides which
    transitions are legal (``josi task run`` moves tasks between them).
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


def record_usage(
    conn, task_id: int, run_id, stage: str, attempt, model, session_id, usage,
) -> None:
    """Store one invocation's usage with its task and run attribution.

    ``usage`` is an :class:`josi.usage.Usage`: its ``None`` fields are stored
    as NULL so an unavailable amount is never confused with a zero one.
    """
    with conn:
        if not _task_exists(conn, task_id):
            raise TaskError("task t" + str(task_id) + " does not exist")
        conn.execute(
            "INSERT INTO usage (task_id, run_id, stage, attempt, model,"
            " session_id, input_tokens, output_tokens, reasoning_tokens,"
            " cache_read_tokens, cache_write_tokens, cost_total, cost_input,"
            " cost_output, cost_source, usage_status, cost_complete, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                run_id,
                stage,
                attempt,
                model,
                session_id,
                usage.input_tokens,
                usage.output_tokens,
                usage.reasoning_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                usage.cost_total,
                usage.cost_input,
                usage.cost_output,
                usage.cost_source,
                USAGE_AVAILABLE if usage.available else USAGE_UNAVAILABLE,
                1 if usage.cost_complete else 0,
                _utc_now(),
            ),
        )


def task_usage(conn, task_id: int) -> list:
    """Return every usage record for ``task_id``, oldest first."""
    rows = conn.execute(
        "SELECT * FROM usage WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    return [_to_usage(row) for row in rows]


def run_usage(conn, run_id: int) -> list:
    """Return every usage record attributed to ``run_id``, oldest first."""
    rows = conn.execute(
        "SELECT * FROM usage WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    return [_to_usage(row) for row in rows]


def usage_totals(records) -> dict:
    """Sum usage records into total, input, and output costs.

    A ``None`` total means no record carried that amount at all; it is never
    reported as a confirmed zero. ``complete`` is False when there are no
    records at all or when any record is missing usage or any of the three
    costs, so callers can flag an incomplete total instead of presenting it
    as exact.
    """
    total = input_cost = output_cost = None
    complete = bool(records)
    for record in records:
        if record.usage_status != "available" or not record.cost_complete:
            complete = False
        if record.cost_total is not None:
            total = (total or 0.0) + record.cost_total
        if record.cost_input is not None:
            input_cost = (input_cost or 0.0) + record.cost_input
        if record.cost_output is not None:
            output_cost = (output_cost or 0.0) + record.cost_output
    return {
        "count": len(records),
        "total": total,
        "input": input_cost,
        "output": output_cost,
        "complete": complete,
    }


def unfinished_dependencies(conn, task_id: int, run_id=None) -> list:
    """Return the ``(id, status, run_id)`` triples of unsatisfied dependencies.

    A dependency is satisfied by ``DONE`` (its approved work reached the base
    branch through an integrated run) or by ``STAGED`` in the run the
    dependent is currently executing in: the dependency's approved commit is
    already in that same run branch. A dependency STAGED in any other run is
    not satisfied - it must first be marked DONE through ``josi run merge``.
    """
    return [
        (row["depends_on_id"], row["status"], row["run_id"])
        for row in conn.execute(
            "SELECT dependencies.depends_on_id AS depends_on_id,"
            " COALESCE(tasks.status, 'missing') AS status,"
            " tasks.run_id AS run_id"
            " FROM dependencies LEFT JOIN tasks"
            " ON tasks.id = dependencies.depends_on_id"
            " WHERE dependencies.task_id = ?"
            " AND COALESCE(tasks.status, '') <> ?"
            " AND NOT (tasks.status = ? AND tasks.run_id IS ?)"
            " ORDER BY dependencies.depends_on_id",
            (task_id, STATUS_DONE, STATUS_STAGED, _run_key(run_id)),
        )
    ]


def _run_key(run_id):
    """Return a value SQLite can compare with ``IS`` against ``run_id``.

    ``run_id`` is None while a batch has not created its run yet; -1 can
    never be a real run id (they start at 1), so it never matches a row.
    """
    return run_id if run_id is not None else -1


def create_run(conn, base_branch: str, start_commit: str, task_ids=()) -> Run:
    """Insert a new RUNNING run and return it.

    The branch name embeds the autoincrement id, so the row is inserted
    first and its branch recorded in the same transaction. ``task_ids`` is
    the run's selection: the tasks it is responsible for finishing before it
    may be marked COMPLETE. The expected commit starts at ``start_commit``
    and only staging advances it to an approved commit.
    """
    now = _utc_now()
    with conn:
        cursor = conn.execute(
            "INSERT INTO runs (branch, base_branch, status, start_commit,"
            " expected_commit, created_at, updated_at)"
            " VALUES ('', ?, ?, ?, ?, ?, ?)",
            (base_branch, RUN_RUNNING, start_commit, start_commit, now, now),
        )
        run_id = cursor.lastrowid
        branch = RUN_BRANCH_PREFIX + "r" + str(run_id)
        conn.execute(
            "UPDATE runs SET branch = ? WHERE id = ?", (branch, run_id)
        )
        _insert_selection(conn, run_id, task_ids)
    return get_run(conn, run_id)


def select_run_tasks(conn, run_id: int, task_ids) -> None:
    """Associate ``task_ids`` with ``run_id`` as part of its selection."""
    get_run(conn, run_id)
    with conn:
        _insert_selection(conn, run_id, task_ids)


def _insert_selection(conn, run_id: int, task_ids) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO run_selection (run_id, task_id) VALUES (?, ?)",
        [(run_id, int(task_id)) for task_id in task_ids],
    )


def selected_tasks(conn, run_id: int) -> list:
    """Return the run's selected tasks, ordered by id."""
    rows = conn.execute(
        "SELECT tasks.* FROM run_selection"
        " JOIN tasks ON tasks.id = run_selection.task_id"
        " WHERE run_selection.run_id = ? ORDER BY tasks.id",
        (run_id,),
    ).fetchall()
    return [_to_task(row, _dependencies(conn, row["id"])) for row in rows]


def list_runs(conn) -> list:
    """Return every run, ordered by id."""
    return [
        _to_run(row)
        for row in conn.execute("SELECT * FROM runs ORDER BY id")
    ]


def get_run(conn, run_id: int) -> Run:
    """Return one run or raise :class:`TaskError` when it does not exist."""
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise TaskError("run r" + str(run_id) + " does not exist")
    return _to_run(row)


def running_run(conn):
    """Return the newest RUNNING run, or None.

    At most one run executes at a time, so a RUNNING run means the project's
    last execution was interrupted before it finished; the next
    ``josi task run`` resumes it instead of creating a new one.
    """
    row = conn.execute(
        "SELECT * FROM runs WHERE status = ? ORDER BY id DESC LIMIT 1",
        (RUN_RUNNING,),
    ).fetchone()
    return _to_run(row) if row is not None else None


def complete_run(conn, run_id: int, final_commit: str) -> Run:
    """Record the run's final commit and mark it COMPLETE.

    COMPLETE means execution stopped: the run branch is frozen at the
    recorded commit and ``josi run merge`` may fast-forward it into the base
    branch (a run stopped by a task failure is completed too, so its staged
    work can be integrated explicitly).
    """
    with conn:
        cursor = conn.execute(
            "UPDATE runs SET status = ?, final_commit = ?, updated_at = ?"
            " WHERE id = ?",
            (RUN_COMPLETE, final_commit, _utc_now(), run_id),
        )
        if cursor.rowcount == 0:
            raise TaskError("run r" + str(run_id) + " does not exist")
    return get_run(conn, run_id)


def record_partial_integration(conn, run_id: int, commit: str) -> Run:
    """Record confirmed Git integration before finalizing task statuses.

    Keeping this durable makes retries safe even if task finalization stops
    midway. The original run selection is never changed.
    """
    with conn:
        conn.execute(
            "UPDATE runs SET partially_integrated = 1, final_commit = ?,"
            " updated_at = ? WHERE id = ?",
            (commit, _utc_now(), run_id),
        )
    return get_run(conn, run_id)


def set_run_status(conn, run_id: int, status: str) -> Run:
    """Set the run's status and return the run."""
    if status not in RUN_STATUSES:
        raise TaskError("invalid run status " + repr(status))
    with conn:
        cursor = conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
            (status, _utc_now(), run_id),
        )
        if cursor.rowcount == 0:
            raise TaskError("run r" + str(run_id) + " does not exist")
    return get_run(conn, run_id)


def run_tasks(conn, run_id: int) -> list:
    """Return the tasks associated with ``run_id``, ordered by id."""
    rows = conn.execute(
        "SELECT * FROM tasks WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    return [_to_task(row, _dependencies(conn, row["id"])) for row in rows]


def stage_task(conn, task_id: int, run_id: int, commit: str) -> Task:
    """Mark the task STAGED in ``run_id``, advancing its expected commit.

    The status change and the expected run tip move in one transaction: the
    run branch may only ever point at an approved commit that was staged
    successfully, never at whatever the branch holds when execution stops.
    """
    now = _utc_now()
    with conn:
        cursor = conn.execute(
            "UPDATE tasks SET status = ?, run_id = ?, updated_at = ?"
            " WHERE id = ?",
            (STATUS_STAGED, run_id, now, task_id),
        )
        if cursor.rowcount == 0:
            raise TaskError("task t" + str(task_id) + " does not exist")
        cursor = conn.execute(
            "UPDATE runs SET expected_commit = ?, updated_at = ? WHERE id = ?",
            (commit, now, run_id),
        )
        if cursor.rowcount == 0:
            raise TaskError("run r" + str(run_id) + " does not exist")
    return get_task(conn, task_id)


def reset_task(conn, task_id: int) -> Task:
    """Reset a task's stored state for a fresh execution and return it.

    The task's status returns to PENDING, its run association is cleared, and
    its stored reviews are deleted, so no stale approval can be staged and the
    task behaves like a newly created one. Dependencies, title, description,
    and timestamps are preserved.

    The worker attempt budget (``.josi/attempts/tN``) and the task branch and
    worktree are owned by :mod:`josi.execution` and are cleared by the caller;
    this function only touches the database.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _task_exists(conn, task_id):
            raise TaskError("task t" + str(task_id) + " does not exist")
        conn.execute(
            "UPDATE tasks SET status = ?, run_id = NULL, updated_at = ?"
            " WHERE id = ?",
            (STATUS_PENDING, _utc_now(), task_id),
        )
        conn.execute("DELETE FROM reviews WHERE task_id = ?", (task_id,))
    return get_task(conn, task_id)


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
        conn.execute("DELETE FROM usage WHERE task_id = ?", (task_id,))
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
        run_id=row["run_id"],
    )


def _to_run(row) -> Run:
    return Run(
        id=row["id"],
        branch=row["branch"],
        base_branch=row["base_branch"],
        status=row["status"],
        start_commit=row["start_commit"],
        final_commit=row["final_commit"],
        expected_commit=row["expected_commit"],
        partially_integrated=bool(row["partially_integrated"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_usage(row) -> UsageRecord:
    return UsageRecord(
        id=row["id"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        stage=row["stage"],
        attempt=row["attempt"],
        model=row["model"],
        session_id=row["session_id"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        cost_total=row["cost_total"],
        cost_input=row["cost_input"],
        cost_output=row["cost_output"],
        cost_source=row["cost_source"],
        usage_status=row["usage_status"],
        cost_complete=bool(row["cost_complete"]),
        created_at=row["created_at"],
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
