"""Command-line interface for actl.

Stage 1 commands:

* ``actl init`` - register the current Git repository as an actl project.
* ``actl plan`` - start or resume the project's single planner session in the
  OpenCode TUI. ``actl plan --new`` starts a fresh session.

Stage 2 commands:

* ``actl task create|list|show|update|delete`` - manage the project's tasks
  in a local SQLite database. They are noninteractive and never touch
  OpenCode.

Stage 3:

* The planner agent manages the same task list through the ``actl task``
  commands. Its agent definition documents the CLI and pre-approves exactly
  those commands, so planning and task management happen in one session.

Stage 4:

* ``actl task run <id>`` executes one task with a noninteractive OpenCode
  worker inside a dedicated Git worktree, under a project-wide execution lock.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, execution, opencode, tasks

PROJECT_DIR = ".actl"
PROJECT_METADATA = "project.json"
PLANNER_STATE = "planner.json"
SCHEMA_VERSION = 1
GITIGNORE_ENTRY = ".actl/"
PLANNER_TITLE = "actl planner"
PLANNER_AGENT_NAME = "actl-planner"
PLANNER_AGENT_PATH = Path(".opencode") / "agents" / (PLANNER_AGENT_NAME + ".md")

# OpenCode loads this file as a custom agent: the frontmatter declares a
# primary agent that can read and explore the project and manage its tasks,
# but never edits it. OpenCode appends these rules after its own broad
# "allow everything" base policy and the last matching rule wins, so the
# catch-all deny must come first and later rules re-allow only the tools the
# planner may use. The format was verified against the installed OpenCode
# 2.1.2: a permission rule is {action, resource, effect} with effect
# allow/deny/ask, action and resource are matched as case-sensitive glob
# patterns (* matches any characters, ? one), the last matching rule wins,
# and a request that matches no rule defaults to "ask". Shell commands are
# checked with action "shell" and the parsed command text as the resource,
# one resource per command in a compound call.
# Everything else - editing, subagents, web access, MCP tools - stays denied,
# including tools that write files under an action name other than "edit".
# "actl task" commands are the only pre-approved shell commands because they
# only read and write .actl/tasks.db. Running tasks ("actl task run") is
# denied outright and comes after the broad allow (last match wins): the
# planner must never launch a worker, only the user runs tasks. The "*>*"
# deny must come after the pre-approval rule (last match wins): OpenCode
# reports a redirected statement as a single resource string, so without the
# deny "actl task ... > src/file" would silently count as an allowed task
# command and could overwrite source files. The .env "ask" rules must follow
# the broad read allow to keep their effect.
# The body becomes the agent's system prompt. Deployed under
# .opencode/agents/ so OpenCode discovers it for this project. actl manages
# this file: ensure_planner_agent() rewrites it when its content differs
# from this definition.
PLANNER_AGENT_DOC = """\
---
description: "Planning assistant that defines requirements and produces plans without modifying code"
mode: primary
permissions:
  - action: "*"
    resource: "*"
    effect: deny
  - action: read
    resource: "*"
    effect: allow
  - action: glob
    resource: "*"
    effect: allow
  - action: grep
    resource: "*"
    effect: allow
  - action: question
    resource: "*"
    effect: allow
  - action: read
    resource: "*.env"
    effect: ask
  - action: read
    resource: "*.env.*"
    effect: ask
  - action: read
    resource: "*.env.example"
    effect: allow
  - action: edit
    resource: "*"
    effect: deny
  - action: shell
    resource: "*"
    effect: ask
  - action: shell
    resource: "actl task *"
    effect: allow
  - action: shell
    resource: "actl task run*"
    effect: deny
  - action: shell
    resource: "*>*"
    effect: deny
---

You are the planning assistant for this repository.

Help the user define software requirements and turn them into a clear,
actionable plan.

- Explore the repository whenever that helps you understand the codebase.
- Inspect and list files with the read, glob, and grep tools instead of shell
  commands such as cat, head, tail, ls, or grep - read pages through large
  files with offset and limit. Those shell commands need confirmation.
- Decompose development goals into concrete, ordered tasks.
- For each task, write a clear description and testable acceptance criteria.
- Ask clarifying questions before proposing a plan, and never start
  implementing.
- Do not modify project code.
- Do not execute implementation tasks. `actl task run` is denied: task
  execution is the user's decision, not yours.

## Managing tasks with actl

The project's tasks live in a SQLite database owned by actl. Manage them
only through the `actl task` commands - never open .actl/tasks.db or any
other internal file with sqlite3, cat, or another tool, and never change
task data by hand. Run the commands from anywhere inside the project; they
print plain text:

- `actl task create --title "TITLE" --description "TEXT" [--depends-on tN ...]`
  creates a PENDING task. Use `--description-file PATH` instead of
  `--description` to read the text from a file.
- `actl task list` prints one line per task: id, status, and title.
- `actl task show tN` prints a task with its description and dependencies.
- `actl task update tN [--title TITLE] [--description TEXT]
  [--description-file PATH] [--depends-on tN ...] [--clear-dependencies]`
  changes a task. `--depends-on` replaces the current dependency set and
  `--clear-dependencies` removes it.
- `actl task delete tN` deletes a task; it fails while other tasks depend
  on it.

Ids are printed as `t1`, `t2`, ... and commands also accept the bare number
(`1`). `--depends-on` may be repeated or comma-separated; a dependency must
reference an existing task, must not be the task itself, and must not create
a dependency cycle. These commands never change a task's status: new tasks
are PENDING, and only the user executes tasks (`actl task run`, which is
denied to you).

Plan with the user before writing tasks:

- Discuss the requirements and the design first, and agree on scope.
- Break the work into ordered, actionable tasks, and use `--depends-on` for
  tasks that must not start before another one is finished.
- Describe the goal and the expected behavior in the task description.
- Write acceptance criteria as a short list of observable, testable results
  (what to run, what to expect).
- Create, update, or delete tasks only when the user asks you to.

## Permissions

Read-only repository inspection (read, glob, grep) and user questions are
allowed. `actl task` shell commands are the only pre-approved shell commands;
every other shell command asks for confirmation, and each command in a
compound call is checked separately - run one command per shell call instead
of chaining them with && or echo separators. `actl task run` is denied
outright: never execute tasks or launch coding workers. Shell redirection is
denied outright: never redirect command output to a file, and avoid the `>`
character in inline task text (for example, write "to" instead of an arrow).
Everything else - editing files, subagents, web access, and MCP tools - is
denied. If a tool is denied, do not look for a workaround; explain the
problem and ask the user.
"""


class ProjectError(Exception):
    """Raised when the project setup is missing or cannot be validated."""


class MetadataError(ProjectError):
    """Raised when actl metadata cannot be read or does not validate."""


def git_toplevel(start: Path) -> Path:
    """Return the root of the Git repository containing ``start``.

    Git prints the path followed by a single newline. Only that terminator is
    removed, so repository names that end in whitespace survive intact;
    stripping all whitespace here would silently redirect actl to a sibling
    directory.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
        )
    except FileNotFoundError:
        raise ProjectError("Git is not installed or not on PATH")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ProjectError("not inside a Git repository; run 'git init' first")
    output = proc.stdout
    if output.endswith(b"\n"):
        output = output[:-1]
        if os.name == "nt" and output.endswith(b"\r"):
            output = output[:-1]
    return Path(os.fsdecode(output)).resolve()


def load_project_metadata(root: Path) -> dict:
    """Load and validate ``.actl/project.json``."""
    path = root / PROJECT_DIR / PROJECT_METADATA
    if not path.is_file():
        raise MetadataError("missing " + str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetadataError(str(path) + " is not readable JSON (" + str(exc) + ")")
    if not isinstance(data, dict):
        raise MetadataError(str(path) + " must contain a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise MetadataError(
            str(path) + " has unsupported schema_version "
            + repr(data.get("schema_version")) + " (expected " + str(SCHEMA_VERSION) + ")"
        )
    if not isinstance(data.get("name"), str):
        raise MetadataError(str(path) + " is missing a valid 'name'")
    return data


def load_planner_session_id(root: Path):
    """Return the active planner session id, or None when none was stored yet."""
    path = root / PROJECT_DIR / PLANNER_STATE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetadataError(str(path) + " is not readable JSON (" + str(exc) + ")")
    session_id = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(session_id, str) or not session_id.startswith("ses"):
        raise MetadataError(str(path) + " does not contain a valid 'session_id'")
    return session_id


def save_planner_session_id(root: Path, session_id: str) -> None:
    path = root / PROJECT_DIR / PLANNER_STATE
    payload = {"session_id": session_id, "created_at": _utc_now()}
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise MetadataError(
            "could not save the planner session state " + str(path)
            + " (" + str(exc) + ")"
        )


def _write_metadata(path: Path, metadata: dict) -> None:
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _parse_worker_model(value: str) -> str:
    """Validate a ``provider/model`` (optionally ``#variant``) worker model."""
    model = value.strip()
    provider, _, rest = model.partition("/")
    if (
        not provider
        or not rest
        or model.startswith("-")
        or any(char.isspace() for char in model)
    ):
        raise ProjectError(
            "invalid worker model " + repr(value)
            + " (expected provider/model, optionally provider/model#variant)"
        )
    return model


def _worker_model(metadata: dict) -> str:
    """Return the project's worker model or raise :class:`ProjectError`."""
    model = metadata.get("worker_model")
    if model is None:
        raise ProjectError(
            "no worker model is configured for this project; run"
            " 'actl init --worker-model provider/model' first"
        )
    if not isinstance(model, str):
        raise ProjectError(
            "project metadata has an invalid 'worker_model' value " + repr(model)
        )
    return _parse_worker_model(model)


def ensure_gitignore(root: Path) -> None:
    """Make sure ``.actl/`` is ignored, preserving existing bytes exactly."""
    path = root / ".gitignore"
    entry = GITIGNORE_ENTRY.encode("utf-8")
    try:
        if path.exists():
            content = path.read_bytes()
            lines = [line.strip() for line in content.splitlines()]
            if entry in lines or entry.rstrip(b"/") in lines:
                return
            separator = b"" if content == b"" or content.endswith(b"\n") else b"\n"
            path.write_bytes(content + separator + entry + b"\n")
        else:
            path.write_bytes(entry + b"\n")
    except OSError as exc:
        raise ProjectError("could not update " + str(path) + " (" + str(exc) + ")")


def ensure_planner_agent(root: Path) -> str:
    """Make the planner agent file match actl's current definition.

    The file is managed by actl: it is written when missing and rewritten
    when its content differs from :data:`PLANNER_AGENT_DOC`, so definition
    updates (for example new permissions) apply on the next plan.
    Returns ``"installed"``, ``"updated"`` or ``"unchanged"``.
    """
    path = root / PLANNER_AGENT_PATH
    existed = path.is_file()
    try:
        if existed and path.read_text(encoding="utf-8") == PLANNER_AGENT_DOC:
            return "unchanged"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PLANNER_AGENT_DOC, encoding="utf-8")
    except OSError as exc:
        raise ProjectError(
            "could not write the planner agent " + str(path) + " (" + str(exc) + ")"
        )
    return "updated" if existed else "installed"


def cmd_init(args) -> int:
    try:
        root = git_toplevel(Path.cwd())
    except ProjectError as exc:
        return _fail(str(exc))
    try:
        opencode.find_binary()
    except opencode.OpenCodeError as exc:
        return _fail(str(exc))
    worker_model = None
    if args.worker_model is not None:
        try:
            worker_model = _parse_worker_model(args.worker_model)
        except ProjectError as exc:
            return _fail(str(exc))

    actl_dir = root / PROJECT_DIR
    metadata_path = actl_dir / PROJECT_METADATA
    if metadata_path.exists():
        try:
            metadata = load_project_metadata(root)
        except MetadataError as exc:
            return _fail(
                "existing actl metadata is invalid (" + str(exc) + "); "
                "remove '" + str(actl_dir) + "' and re-run 'actl init'"
            )
        if worker_model is not None and metadata.get("worker_model") != worker_model:
            metadata["worker_model"] = worker_model
            try:
                _write_metadata(metadata_path, metadata)
            except OSError as exc:
                return _fail(
                    "could not write " + str(metadata_path) + " (" + str(exc) + ")"
                )
            print("Updated the worker model to " + worker_model)
        else:
            print("actl is already initialized in " + str(root))
    else:
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "name": root.name,
            "created_at": _utc_now(),
        }
        if worker_model is not None:
            metadata["worker_model"] = worker_model
        try:
            actl_dir.mkdir(parents=True, exist_ok=True)
            _write_metadata(metadata_path, metadata)
        except OSError as exc:
            return _fail(
                "could not write " + str(metadata_path) + " (" + str(exc) + ")"
            )
        print("Initialized actl in " + str(root))

    # Creating the task database here (and again, idempotently, on every task
    # command) is safe: the schema uses CREATE TABLE IF NOT EXISTS, so
    # existing tasks survive repeated initialization.
    try:
        conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
        conn.close()
    except tasks.TaskError as exc:
        return _fail(str(exc))

    try:
        ensure_gitignore(root)
    except ProjectError as exc:
        return _fail(str(exc))
    print("Next step: run 'actl plan' to start a planning session.")
    return 0


def _select_planner_agent(binary: str, root: Path, session: opencode.Session) -> None:
    """Switch ``session`` to the planner agent when it is not already selected.

    Raises :class:`ProjectError` when the agent cannot be selected.
    """
    if session.agent == PLANNER_AGENT_NAME:
        return
    try:
        opencode.switch_agent(binary, root, session.id, PLANNER_AGENT_NAME)
    except opencode.OpenCodeError as exc:
        raise ProjectError(
            "cannot select the planner agent for session '"
            + session.id + "': " + str(exc)
        )


def _project_root() -> Path:
    """Return the project root, requiring a valid initialized actl project.

    The Git repository root is resolved first and must contain a valid
    ``.actl/project.json``, so no command can read or write another project's
    state - not even from a nested repository.
    """
    root = git_toplevel(Path.cwd())
    if not (root / PROJECT_DIR).is_dir():
        raise ProjectError(
            "no actl project found at the Git repository root " + str(root)
            + "; run 'actl init' first"
        )
    try:
        load_project_metadata(root)
    except MetadataError as exc:
        raise ProjectError("invalid actl project metadata: " + str(exc))
    return root


def cmd_plan(args) -> int:
    try:
        root = _project_root()
    except ProjectError as exc:
        return _fail(str(exc))
    try:
        binary = opencode.find_binary()
    except opencode.OpenCodeError as exc:
        return _fail(str(exc))
    try:
        agent_status = ensure_planner_agent(root)
    except ProjectError as exc:
        return _fail(str(exc))
    if agent_status == "updated":
        print(
            "Updated the planner agent definition ("
            + str(root / PLANNER_AGENT_PATH) + ")"
        )

    session_id = None
    if not args.new:
        try:
            session_id = load_planner_session_id(root)
        except MetadataError as exc:
            return _fail(
                "cannot resume the planner session: " + str(exc) + "; "
                "run 'actl plan --new' to start a fresh session"
            )

    if session_id is not None:
        try:
            session = opencode.get_session(binary, root, session_id)
        except opencode.OpenCodeError as exc:
            return _fail(
                "cannot verify planner session '" + session_id + "': " + str(exc) + "; "
                "run 'actl plan --new' to start a fresh session"
            )
        try:
            _select_planner_agent(binary, root, session)
        except ProjectError as exc:
            return _fail(str(exc))
        print(
            "Resuming planner session " + session.id
            + " (agent: " + PLANNER_AGENT_NAME + ") in " + str(root)
        )
        return _launch_tui(binary, root, session.id)

    try:
        session = opencode.create_session(
            binary, root, title=PLANNER_TITLE, agent=PLANNER_AGENT_NAME
        )
    except opencode.OpenCodeError as exc:
        return _fail("could not create an OpenCode session: " + str(exc))
    try:
        _select_planner_agent(binary, root, session)
        save_planner_session_id(root, session.id)
    except ProjectError as exc:
        return _fail(str(exc))
    print(
        "Started planner session " + session.id
        + " (agent: " + PLANNER_AGENT_NAME + ") in " + str(root)
    )
    return _launch_tui(binary, root, session.id)


def _launch_tui(binary: str, root: Path, session_id: str) -> int:
    sys.stdout.flush()
    try:
        returncode = opencode.launch_tui(binary, root, session_id)
    except opencode.OpenCodeError as exc:
        return _fail(str(exc))
    if returncode != 0:
        print("actl: error: OpenCode exited with status " + str(returncode), file=sys.stderr)
        return returncode
    return 0


@contextmanager
def _task_database():
    """Yield a connection to the current project's task database.

    The project root is validated first (see :func:`_project_root`), so a task
    command can never read or write another project's database. The database
    file is created (with the current schema) when it does not exist yet.
    """
    root = _project_root()
    conn = tasks.connect(root / PROJECT_DIR / tasks.DATABASE_NAME)
    try:
        yield conn
    finally:
        conn.close()


def cmd_task_create(args) -> int:
    try:
        description = _resolve_description(args)
        depends_on = _parse_dependencies(args.depends_on)
        with _task_database() as conn:
            task = tasks.create_task(
                conn, args.title, description if description is not None else "",
                depends_on,
            )
    except (ProjectError, tasks.TaskError, sqlite3.Error) as exc:
        return _fail(str(exc))
    print("Created task t" + str(task.id))
    return 0


def cmd_task_list(_args) -> int:
    try:
        with _task_database() as conn:
            items = tasks.list_tasks(conn)
    except (ProjectError, tasks.TaskError, sqlite3.Error) as exc:
        return _fail(str(exc))
    for task in items:
        print("t" + str(task.id) + "  " + task.status + "  " + task.title)
    return 0


def cmd_task_show(args) -> int:
    try:
        task_id = _parse_task_id(args.id)
        with _task_database() as conn:
            task = tasks.get_task(conn, task_id)
    except (ProjectError, tasks.TaskError, sqlite3.Error) as exc:
        return _fail(str(exc))
    _print_task(task)
    return 0


def cmd_task_update(args) -> int:
    try:
        task_id = _parse_task_id(args.id)
        description = _resolve_description(args)
        if args.clear_dependencies:
            depends_on = []
        elif args.depends_on is not None:
            depends_on = _parse_dependencies(args.depends_on)
        else:
            depends_on = None
        with _task_database() as conn:
            task = tasks.update_task(
                conn, task_id, title=args.title, description=description,
                depends_on=depends_on,
            )
    except (ProjectError, tasks.TaskError, sqlite3.Error) as exc:
        return _fail(str(exc))
    print("Updated task t" + str(task.id))
    return 0


def cmd_task_delete(args) -> int:
    try:
        task_id = _parse_task_id(args.id)
        with _task_database() as conn:
            tasks.delete_task(conn, task_id)
    except (ProjectError, tasks.TaskError, sqlite3.Error) as exc:
        return _fail(str(exc))
    print("Deleted task t" + str(task_id))
    return 0


def cmd_task_run(args) -> int:
    """Execute one task with the OpenCode worker."""
    try:
        task_id = _parse_task_id(args.id)
        root = _project_root()
        metadata = load_project_metadata(root)
        model = _worker_model(metadata)
        binary = opencode.find_binary()
    except (ProjectError, tasks.TaskError, opencode.OpenCodeError) as exc:
        return _fail(str(exc))
    actl_dir = root / PROJECT_DIR
    try:
        with execution.lock(actl_dir) as lock_fd:
            return _execute_task(binary, root, actl_dir, task_id, model, lock_fd)
    except (
        ProjectError, tasks.TaskError, sqlite3.Error,
        execution.ExecutionError, opencode.OpenCodeError,
    ) as exc:
        return _fail(str(exc))


def _execute_task(binary, root, actl_dir, task_id, model, lock_fd) -> int:
    """Run one task while the execution lock is held.

    The task is marked RUNNING before the worktree is prepared, so an
    interrupted attempt keeps RUNNING and a later explicit run continues from
    its worktree. A completed attempt always ends as IMPLEMENTED or FAILED.
    """
    task = _load_task(actl_dir, task_id)
    _require_runnable(task)
    if task.status == tasks.STATUS_RUNNING:
        print(
            "Retrying task t" + str(task.id)
            + ", left RUNNING by an interrupted execution"
        )

    log_path, log = execution.open_run_log(actl_dir, task.id)
    with log:
        log.write("actl task run\n")
        log.write("task: t" + str(task.id) + " - " + task.title + "\n")
        log.write("model: " + model + "\n")
        log.write("started_at: " + _utc_now() + "\n")

        _set_task_status(actl_dir, task.id, tasks.STATUS_RUNNING)
        status = tasks.STATUS_FAILED
        reason = ""
        exit_code = None
        try:
            worktree = execution.ensure_worktree(
                root, actl_dir, task, retry=task.status != tasks.STATUS_PENDING
            )
            log.write("branch: " + worktree.branch + "\n")
            log.write("worktree: " + str(worktree.path) + "\n")
            if worktree.created:
                print(
                    "Created worktree " + str(worktree.path)
                    + " (branch " + worktree.branch + ")"
                )
            else:
                print(
                    "Reusing worktree " + str(worktree.path)
                    + " (branch " + worktree.branch + ")"
                )
            session = opencode.create_session(
                binary, worktree.path,
                title="actl task t" + str(task.id) + ": " + task.title,
                agent=opencode.WORKER_AGENT,
            )
            log.write("session: " + session.id + "\n")
            log.write("---\n")
            # Flush before the worker starts writing to the same file through
            # its inherited stdout descriptor, so the header stays on top.
            log.flush()
            print("Running task t" + str(task.id) + " with model " + model)
            exit_code = opencode.run_headless(
                binary, worktree.path, session.id, model,
                execution.worker_prompt(task, worktree), log, pass_fds=(lock_fd,),
            )
            status, reason = _evaluate_run(worktree, exit_code)
        except (execution.ExecutionError, opencode.OpenCodeError, OSError) as exc:
            reason = str(exc)

        log.write("---\n")
        log.write("finished_at: " + _utc_now() + "\n")
        if exit_code is not None:
            log.write("exit_code: " + str(exit_code) + "\n")
        log.write("result: " + status + "\n")
        if reason:
            log.write("reason: " + reason + "\n")

    _set_task_status(actl_dir, task.id, status)
    if status == tasks.STATUS_IMPLEMENTED:
        print("Task t" + str(task.id) + " implemented (log: " + str(log_path) + ")")
        return 0
    return _fail(
        "task t" + str(task.id) + " failed: " + reason
        + " (log: " + str(log_path) + ")"
    )


def _evaluate_run(worktree, exit_code: int):
    """Turn a finished worker run into ``(status, reason)``."""
    if exit_code != 0:
        return (
            tasks.STATUS_FAILED,
            "the OpenCode worker exited with status " + str(exit_code),
        )
    branch = execution.worktree_branch(worktree.path)
    if branch != worktree.branch:
        return (
            tasks.STATUS_FAILED,
            "the worker left the worktree on '" + branch
            + "' instead of '" + worktree.branch + "'",
        )
    dirty = execution.worktree_dirty(worktree.path)
    if dirty:
        return (
            tasks.STATUS_FAILED,
            "the worker left the worktree dirty (commit or remove the changes"
            " and run the task again)",
        )
    return tasks.STATUS_IMPLEMENTED, ""


def _require_runnable(task) -> None:
    """Refuse tasks that may not be executed in this stage."""
    if task.depends_on:
        dependencies = ", ".join("t" + str(dep) for dep in task.depends_on)
        raise tasks.TaskError(
            "cannot run task t" + str(task.id) + ": it depends on " + dependencies
            + "; dependency-aware execution arrives in a later stage (Stage 7"
            " introduces DONE after merging)"
        )
    if task.status == tasks.STATUS_IMPLEMENTED:
        raise tasks.TaskError(
            "cannot run task t" + str(task.id) + ": it is already IMPLEMENTED"
        )
    if task.status not in (
        tasks.STATUS_PENDING, tasks.STATUS_RUNNING, tasks.STATUS_FAILED
    ):
        raise tasks.TaskError(
            "cannot run task t" + str(task.id)
            + ": unexpected status " + repr(task.status)
        )


def _load_task(actl_dir: Path, task_id: int):
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        return tasks.get_task(conn, task_id)
    finally:
        conn.close()


def _set_task_status(actl_dir: Path, task_id: int, status: str) -> None:
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        tasks.set_task_status(conn, task_id, status)
    finally:
        conn.close()


def _resolve_description(args):
    """Return the requested description text, or None when none was given."""
    if args.description_file is not None:
        return _read_description_file(args.description_file)
    return args.description


def _read_description_file(path_text: str) -> str:
    path = Path(path_text)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise tasks.TaskError(
            "cannot read description file " + str(path) + " (" + str(exc) + ")"
        )
    except UnicodeDecodeError as exc:
        raise tasks.TaskError(
            "description file " + str(path) + " is not valid UTF-8 (" + str(exc) + ")"
        )
    return content.rstrip("\n")


_TASK_ID_RE = re.compile(r"[tT]?([0-9]+)")


def _parse_task_id(value: str) -> int:
    match = _TASK_ID_RE.fullmatch(value.strip())
    if match is None:
        raise tasks.TaskError(
            "invalid task id " + repr(value) + " (expected tN or N, e.g. t1)"
        )
    task_id = int(match.group(1))
    if task_id == 0:
        raise tasks.TaskError("invalid task id " + repr(value) + " (ids start at t1)")
    return task_id


def _parse_dependencies(values) -> list:
    """Parse repeated or comma-separated ``--depends-on`` values."""
    dependencies = []
    for value in values or []:
        for part in value.split(","):
            dependencies.append(_parse_task_id(part.strip()))
    return dependencies


def _print_task(task) -> None:
    print("id: t" + str(task.id))
    print("title: " + task.title)
    print("status: " + task.status)
    print("created_at: " + task.created_at)
    print("updated_at: " + task.updated_at)
    print(
        "depends_on: "
        + (", ".join("t" + str(dep) for dep in task.depends_on) or "(none)")
    )
    print("description:")
    print(task.description if task.description else "(none)")


def _fail(message: str) -> int:
    print("actl: error: " + message, file=sys.stderr)
    return 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="actl",
        description="Orchestrate AI coding agents through OpenCode.",
    )
    parser.add_argument("--version", action="version", version="actl " + __version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser(
        "init", help="register the current Git repository as an actl project"
    )
    init.add_argument(
        "--worker-model", metavar="MODEL",
        help="set the project's worker model (provider/model[#variant])",
    )
    plan = subparsers.add_parser(
        "plan", help="start or resume the project's planner session"
    )
    plan.add_argument(
        "--new", action="store_true", help="start a fresh planner session"
    )

    task = subparsers.add_parser("task", help="manage project tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)

    create = task_sub.add_parser("create", help="create a task")
    create.add_argument("--title", required=True, help="task title")
    _add_description_arguments(create)
    create.add_argument(
        "--depends-on", action="append", metavar="ID",
        help="task this task depends on (tN or N); repeatable or comma-separated",
    )

    task_sub.add_parser("list", help="list tasks")

    show = task_sub.add_parser("show", help="show one task")
    show.add_argument("id", help="task id (tN or N)")

    update = task_sub.add_parser("update", help="update a task")
    update.add_argument("id", help="task id (tN or N)")
    update.add_argument("--title", help="new title")
    _add_description_arguments(update)
    dependencies = update.add_mutually_exclusive_group()
    dependencies.add_argument(
        "--depends-on", action="append", metavar="ID",
        help="replace the task's dependencies; repeatable or comma-separated",
    )
    dependencies.add_argument(
        "--clear-dependencies", action="store_true",
        help="remove all dependencies",
    )

    delete = task_sub.add_parser("delete", help="delete a task")
    delete.add_argument("id", help="task id (tN or N)")

    run = task_sub.add_parser(
        "run", help="execute a task in an isolated Git worktree"
    )
    run.add_argument("id", help="task id (tN or N)")
    return parser


def _add_description_arguments(parser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--description", help="task description")
    group.add_argument(
        "--description-file", metavar="PATH",
        help="read the description from a file (trailing newlines are stripped)",
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "plan":
        return cmd_plan(args)
    handlers = {
        "create": cmd_task_create,
        "list": cmd_task_list,
        "show": cmd_task_show,
        "update": cmd_task_update,
        "delete": cmd_task_delete,
        "run": cmd_task_run,
    }
    return handlers[args.task_command](args)
