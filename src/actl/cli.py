"""Command-line interface for actl.

Stage 1 commands:

* ``actl init`` - register the current Git repository as an actl project and
  record its base branch (``--base``, or detected/selected automatically).
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

Stage 5:

* After the worker succeeds, the configured validation commands run inside the
  task worktree, and an independent reviewer - in a temporary worktree at the
  validated commit - returns a structured verdict. The task ends APPROVED or
  FAILED. A reviewer model is required for every run, and the worker model
  whenever the worker runs; without a reviewer model the run refuses before
  launching anything instead of skipping review.

Stage 6:

* A validation failure or a reviewer rejection with critical or major findings
  triggers automatic repairs: up to three completed worker attempts. The first
  two use the project's worker model, the third the project's escalation
  worker model (required, like the reviewer model); every attempt continues in
  the same branch and worktree and receives the blocking findings or the
  failed validation command as feedback. If the third attempt is still not
  approved, the task ends NEEDS_HUMAN. Only implementation failures trigger
  repairs: worker and reviewer execution errors end the run with FAILED, and
  an explicit retry after a reviewer infrastructure failure validates and
  reviews the committed implementation again without spending another worker
  attempt. An interrupted attempt is retried under the same number and does
  not consume the budget. The attempt budget is persisted per task in
  ``.actl/attempts/`` so restarting actl never resets it.

Stage 7:

* Execution is grouped into runs. ``actl task run`` creates (or resumes) one
  run branch ``actl/run/rN`` and worktree from the configured base branch and
  creates every task branch from that run branch, so each task starts from the
  previous staged task's result. An approved task is fast-forwarded into the
  run branch and becomes STAGED; the base branch is never touched during
  execution. ``actl task run`` accepts several task ids or ``--all`` and
  executes them strictly sequentially under one execution lock, in the given
  order or in deterministic dependency order. ``actl run merge rN``
  fast-forwards a completed run into the configured base branch and turns its
  STAGED tasks DONE; it is idempotent and also detects a manual merge of the
  run branch. A dependency is satisfied by DONE or by STAGED in the run the
  dependent is currently executing in; a dependency staged in another run
  must be integrated first.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, execution, opencode, review, tasks, validation

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
# The task management subcommands (create, list, show, update, delete) are the
# only pre-approved shell commands because they only read and write
# .actl/tasks.db. A broad "actl task *" deny comes before those allows (last
# match wins), and the management allows re-enable only the documented forms;
# "actl task run" - including whitespace variants such as "actl task  run" -
# stays denied, so the planner can never launch a worker. "actl run *" is
# denied outright: integrating a run into the base branch is the user's
# decision, never the planner's. The "*>*" deny must
# come after the management allows (last match wins): OpenCode reports a
# redirected statement as a single resource string, so without the deny
# "actl task ... > src/file" would silently count as an allowed task command
# and could overwrite source files. The .env "ask" rules must follow the broad
# read allow to keep their effect.
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
    effect: deny
  - action: shell
    resource: "actl task create *"
    effect: allow
  - action: shell
    resource: "actl task list *"
    effect: allow
  - action: shell
    resource: "actl task show *"
    effect: allow
  - action: shell
    resource: "actl task update *"
    effect: allow
  - action: shell
    resource: "actl task delete *"
    effect: allow
  - action: shell
    resource: "actl run *"
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
- Do not execute implementation tasks or integrate runs. `actl task run` and
  `actl run` are denied: task execution and run integration are the user's
  decision, not yours.

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
allowed. The `actl task create`, `actl task list`, `actl task show`,
`actl task update`, and `actl task delete` subcommands are the only
pre-approved shell commands; write them with single spaces between the
command words. Every other shell command asks for
confirmation, and each command in a compound call is checked separately - run
one command per shell call instead of chaining them with && or echo
separators. `actl task run` and `actl run` are denied outright: never execute
tasks, launch coding workers, or integrate a run into the base branch. Shell
redirection is denied outright: never redirect command
output to a file, and avoid the `>` character in inline task text (for
example, write "to" instead of an arrow).
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
    base_branch = data.get("base_branch")
    if base_branch is not None and (
        not isinstance(base_branch, str) or not base_branch.strip()
    ):
        raise MetadataError(
            str(path) + " has an invalid 'base_branch' value " + repr(base_branch)
        )
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


def _parse_model(value: str, role: str) -> str:
    """Validate a ``provider/model`` (optionally ``#variant``) model name."""
    model = value.strip()
    provider, _, rest = model.partition("/")
    if (
        not provider
        or not rest
        or model.startswith("-")
        or any(char.isspace() for char in model)
    ):
        raise ProjectError(
            "invalid " + role + " model " + repr(value)
            + " (expected provider/model, optionally provider/model#variant)"
        )
    return model


def _parse_worker_model(value: str) -> str:
    return _parse_model(value, "worker")


def _parse_reviewer_model(value: str) -> str:
    return _parse_model(value, "reviewer")


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


def _reviewer_model(metadata: dict) -> str:
    """Return the project's reviewer model or raise :class:`ProjectError`.

    Review is a required part of ``actl task run``: without a reviewer model
    the command refuses before launching anything instead of silently
    skipping the independent review.
    """
    model = metadata.get("reviewer_model")
    if model is None:
        raise ProjectError(
            "no reviewer model is configured for this project; run"
            " 'actl init --reviewer-model provider/model' first"
        )
    if not isinstance(model, str):
        raise ProjectError(
            "project metadata has an invalid 'reviewer_model' value " + repr(model)
        )
    return _parse_reviewer_model(model)


def _escalation_worker_model(metadata: dict) -> str:
    """Return the project's escalation worker model or raise.

    The third (final) worker attempt of every repair cycle uses this model.
    It is required by ``actl task run`` from the start, exactly like the
    reviewer model: escalation is part of the design, so a project defines it
    before a task runs instead of discovering that it is missing when the
    final attempt is due. Without it the run refuses before any lock,
    worktree, or task change.
    """
    model = metadata.get("escalation_worker_model")
    if model is None:
        raise ProjectError(
            "no escalation worker model is configured for this project; run"
            " 'actl init --escalation-worker-model provider/model' first"
        )
    if not isinstance(model, str):
        raise ProjectError(
            "project metadata has an invalid 'escalation_worker_model' value "
            + repr(model)
        )
    return _parse_model(model, "escalation worker")


def _validate_explicit_base_branch(root: Path, value: str) -> str:
    """Validate ``--base`` and return the branch name.

    The branch must be a literal, valid branch name. In a repository that has
    commits it must also already exist locally; in a repository without
    commits an intended branch is accepted because it has no ref yet.
    """
    branch = value.strip()
    if not execution.valid_branch_name(root, branch):
        raise ProjectError(
            "invalid base branch name " + repr(value)
            + " (expected a branch name such as 'main' or 'develop')"
        )
    if execution.repo_has_commits(root) and not execution.branch_exists(root, branch):
        raise ProjectError(
            "base branch '" + branch + "' does not exist in " + str(root)
            + " (create it first or pick an existing branch)"
        )
    return branch


def _prompt_for_base_branch(root: Path) -> str:
    """Ask the user to pick the base branch from the existing branches."""
    branches = execution.local_branches(root)
    if len(branches) == 1:
        return branches[0]
    if not branches:
        raise ProjectError(
            "cannot determine the base branch: the repository has no branches;"
            " run 'actl init --base <branch>'"
        )
    print("The base branch could not be determined automatically.")
    print("Existing branches:")
    for index, branch in enumerate(branches, 1):
        print("  " + str(index) + ") " + branch)
    while True:
        try:
            answer = input("Base branch (number or name): ").strip()
        except EOFError:
            raise ProjectError(
                "cannot determine the base branch; select one with"
                " 'actl init --base <branch>'"
            )
        if answer.isdigit() and 1 <= int(answer) <= len(branches):
            return branches[int(answer) - 1]
        if answer in branches:
            return answer
        print(
            "Enter a number between 1 and " + str(len(branches))
            + ", or one of the branch names above."
        )


def _resolve_new_base_branch(root: Path, requested) -> str:
    """Choose the base branch for ``init`` when none is stored yet.

    Priority: the explicit ``--base`` argument, the remote default branch
    (``origin/HEAD``), the single branch among ``main`` and ``master``, then
    a question listing the existing branches. A repository without commits
    uses the unborn branch ``HEAD`` points at unless ``--base`` was given.
    """
    if requested is not None:
        return _validate_explicit_base_branch(root, requested)
    if not execution.repo_has_commits(root):
        unborn = execution.unborn_branch(root)
        if unborn is None:
            raise ProjectError(
                "cannot determine the base branch of a repository without"
                " commits (HEAD is not on a branch); run 'actl init --base"
                " <branch>'"
            )
        return unborn
    detected = execution.detect_base_branch(root)
    if detected is not None:
        return detected
    return _prompt_for_base_branch(root)


def _run_base_branch(root: Path, metadata: dict) -> str:
    """Return the base branch for ``actl task run``.

    A project may predate the stored ``base_branch`` field; such a project
    keeps working by detecting the branch, without silently writing metadata.
    """
    base_branch = metadata.get("base_branch")
    if base_branch is not None:
        if not execution.valid_branch_name(root, base_branch):
            raise ProjectError(
                "project metadata has an invalid 'base_branch' value "
                + repr(base_branch)
            )
        return base_branch
    detected = execution.effective_base_branch(root)
    if detected is None:
        raise ProjectError(
            "no base branch is configured for this project and none could be"
            " detected; run 'actl init --base <branch>'"
        )
    print(
        "Using detected base branch '" + detected
        + "' (run 'actl init --base <branch>' to configure it)"
    )
    return detected


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
    reviewer_model = None
    if args.reviewer_model is not None:
        try:
            reviewer_model = _parse_reviewer_model(args.reviewer_model)
        except ProjectError as exc:
            return _fail(str(exc))
    escalation_worker_model = None
    if args.escalation_worker_model is not None:
        try:
            escalation_worker_model = _parse_model(
                args.escalation_worker_model, "escalation worker"
            )
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
        updates = {}
        if worker_model is not None and metadata.get("worker_model") != worker_model:
            updates["worker_model"] = worker_model
        if (
            reviewer_model is not None
            and metadata.get("reviewer_model") != reviewer_model
        ):
            updates["reviewer_model"] = reviewer_model
        if (
            escalation_worker_model is not None
            and metadata.get("escalation_worker_model") != escalation_worker_model
        ):
            updates["escalation_worker_model"] = escalation_worker_model
        stored_base = metadata.get("base_branch")
        if args.base is not None:
            # An explicit --base is the user's decision: it may replace an
            # earlier selection, but a plain init never does.
            try:
                base_branch = _validate_explicit_base_branch(root, args.base)
            except ProjectError as exc:
                return _fail(str(exc))
            if stored_base != base_branch:
                updates["base_branch"] = base_branch
        elif stored_base is None:
            # Projects initialized before base_branch existed gain a
            # selection once; from then on it is never changed silently.
            try:
                updates["base_branch"] = _resolve_new_base_branch(root, None)
            except ProjectError as exc:
                return _fail(str(exc))
        if updates:
            metadata.update(updates)
            try:
                _write_metadata(metadata_path, metadata)
            except OSError as exc:
                return _fail(
                    "could not write " + str(metadata_path) + " (" + str(exc) + ")"
                )
            if "worker_model" in updates:
                print("Updated the worker model to " + updates["worker_model"])
            if "reviewer_model" in updates:
                print("Updated the reviewer model to " + updates["reviewer_model"])
            if "escalation_worker_model" in updates:
                print(
                    "Updated the escalation worker model to "
                    + updates["escalation_worker_model"]
                )
            if "base_branch" in updates:
                print("Updated the base branch to " + updates["base_branch"])
        else:
            print("actl is already initialized in " + str(root))
    else:
        try:
            base_branch = _resolve_new_base_branch(root, args.base)
        except ProjectError as exc:
            return _fail(str(exc))
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "name": root.name,
            "created_at": _utc_now(),
            "base_branch": base_branch,
        }
        if worker_model is not None:
            metadata["worker_model"] = worker_model
        if reviewer_model is not None:
            metadata["reviewer_model"] = reviewer_model
        if escalation_worker_model is not None:
            metadata["escalation_worker_model"] = escalation_worker_model
        try:
            actl_dir.mkdir(parents=True, exist_ok=True)
            _write_metadata(metadata_path, metadata)
        except OSError as exc:
            return _fail(
                "could not write " + str(metadata_path) + " (" + str(exc) + ")"
            )
        print("Initialized actl in " + str(root))
        print("Base branch: " + base_branch)

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


def cmd_run_list(_args) -> int:
    """List the project's runs: id, status, branch, and staged tasks."""
    try:
        with _run_database() as conn:
            for run in tasks.list_runs(conn):
                task_ids = [
                    "t" + str(task.id) for task in tasks.run_tasks(conn, run.id)
                ]
                print(
                    "r" + str(run.id) + "  " + run.status + "  " + run.branch
                    + "  " + (", ".join(task_ids) or "(no tasks)")
                )
    except (ProjectError, tasks.TaskError, sqlite3.Error) as exc:
        return _fail(str(exc))
    return 0


def cmd_run_show(args) -> int:
    """Show one run with its commits and associated tasks."""
    try:
        run_id = _parse_run_id(args.id)
        with _run_database() as conn:
            run = tasks.get_run(conn, run_id)
            associated = tasks.run_tasks(conn, run.id)
    except (ProjectError, tasks.TaskError, sqlite3.Error) as exc:
        return _fail(str(exc))
    print("id: r" + str(run.id))
    print("status: " + run.status)
    print("branch: " + run.branch)
    print("base_branch: " + run.base_branch)
    print("start_commit: " + run.start_commit)
    print("final_commit: " + (run.final_commit or "(none)"))
    print("created_at: " + run.created_at)
    print("updated_at: " + run.updated_at)
    print("tasks:")
    if not associated:
        print("(none)")
    for task in associated:
        print("t" + str(task.id) + "  " + task.status + "  " + task.title)
    return 0


def cmd_run_merge(args) -> int:
    """Integrate a completed run into the configured base branch."""
    try:
        run_id = _parse_run_id(args.id)
        root = _project_root()
        metadata = load_project_metadata(root)
        base_branch = _run_base_branch(root, metadata)
        actl_dir = root / PROJECT_DIR
    except (ProjectError, tasks.TaskError) as exc:
        return _fail(str(exc))
    try:
        with execution.lock(actl_dir):
            return _merge_run(root, actl_dir, run_id, base_branch)
    except (
        ProjectError, tasks.TaskError, sqlite3.Error,
        execution.ExecutionError,
    ) as exc:
        return _fail(str(exc))


@contextmanager
def _run_database():
    """Yield a connection to the current project's run database."""
    root = _project_root()
    conn = tasks.connect(root / PROJECT_DIR / tasks.DATABASE_NAME)
    try:
        yield conn
    finally:
        conn.close()


def _merge_run(root: Path, actl_dir: Path, run_id: int, base_branch: str) -> int:
    """Fast-forward a COMPLETE run into the base branch; idempotent.

    Three cases keep the command safe to repeat:

    * the run was not integrated yet: the run branch must still point at the
      recorded final commit, and only a fast-forward merge into a clean base
      worktree is attempted;
    * the recorded final commit is already an ancestor of the base branch
      (a manual merge, with or without a merge commit, or a repeated
      command): the Git merge is skipped and only the statuses are finalized;
    * the fast-forward cannot succeed (the base branch advanced with its own
      commits): nothing changes, the user integrates the run by hand and
      reruns the command to finalize the statuses.

    STAGED tasks of the run become DONE only here, never during execution.
    """
    run = _load_run(actl_dir, run_id)
    if run.status == tasks.RUN_INTEGRATED:
        done = _finish_run_tasks(actl_dir, run)
        print(
            "Run r" + str(run.id) + " is already integrated into " + base_branch
            + (("; marked " + ", ".join("t" + str(i) for i in done) + " DONE")
               if done else "")
        )
        return 0
    if run.status != tasks.RUN_COMPLETE:
        raise tasks.TaskError(
            "cannot integrate run r" + str(run.id) + ": its execution is still"
            " marked " + run.status + "; run 'actl task run ...' again to"
            " resume and finish it first"
        )
    if not run.final_commit:
        raise tasks.TaskError(
            "cannot integrate run r" + str(run.id)
            + ": no final commit was recorded"
        )
    if not execution.branch_exists(root, base_branch):
        raise tasks.TaskError(
            "base branch '" + base_branch + "' does not exist in " + str(root)
            + "; refusing to merge"
        )
    current = execution.ref_commit(root, "refs/heads/" + run.branch)
    if current != run.final_commit:
        raise tasks.TaskError(
            "run branch " + run.branch + " moved after the run finished:"
            " expected " + run.final_commit[:12] + ", found " + current[:12]
            + "; refusing to merge"
        )
    if execution.is_ancestor(
        root, run.final_commit, "refs/heads/" + base_branch
    ):
        print(
            "Run r" + str(run.id) + " is already integrated into " + base_branch
            + " (merged by hand); finalizing the statuses."
        )
    else:
        _event("Fast-forward merging run r" + str(run.id) + "...")
        try:
            execution.integrate_commit(
                root, actl_dir, base_branch, run.branch, run.final_commit,
            )
        except execution.ExecutionError as exc:
            print("")
            print("Run r" + str(run.id) + " cannot be integrated automatically:")
            print("  " + str(exc))
            print("")
            print("Integrate the run branch by hand and commit the result, then")
            print("run 'actl run merge r" + str(run.id) + "' again to finalize"
                  " its status:")
            print(
                "    git checkout " + base_branch + " && git merge " + run.branch
            )
            _fail(
                "run r" + str(run.id) + " could not be fast-forwarded into "
                + base_branch + " (manual Git integration required)"
            )
            return 1
        print(
            "Fast-forwarded run r" + str(run.id) + " into " + base_branch + "."
        )
    done = _finish_run_tasks(actl_dir, run)
    _set_run_status(actl_dir, run.id, tasks.RUN_INTEGRATED)
    print(
        "Run r" + str(run.id) + " integrated into " + base_branch
        + ("; " + ", ".join("t" + str(i) for i in done) + " \u2192 DONE"
           if done else "")
    )
    return 0


def _finish_run_tasks(actl_dir: Path, run) -> list:
    """Mark every STAGED task of ``run`` DONE; return the task ids."""
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        done = []
        for task in tasks.run_tasks(conn, run.id):
            if task.status == tasks.STATUS_STAGED:
                tasks.set_task_status(conn, task.id, tasks.STATUS_DONE)
                done.append(task.id)
        return done
    finally:
        conn.close()


#: Statuses ``actl task run --all`` selects for execution. STAGED tasks are
#: already in a run branch, DONE tasks are already integrated, and NEEDS_HUMAN
#: tasks need a human, so none is selected automatically; every other status
#: the single-task lifecycle accepts stays eligible.
_RUNNABLE_STATUSES = (
    tasks.STATUS_PENDING, tasks.STATUS_RUNNING, tasks.STATUS_IMPLEMENTED,
    tasks.STATUS_FAILED, tasks.STATUS_NEEDS_CHANGES, tasks.STATUS_APPROVED,
)


def cmd_task_run(args) -> int:
    """Execute tasks inside a run: worker, validation, review, and staging."""
    try:
        if args.all_tasks:
            if args.ids:
                raise tasks.TaskError("specify task ids or --all, not both")
            requested = None
        else:
            if not args.ids:
                raise tasks.TaskError(
                    "specify at least one task id (tN or N) or --all"
                )
            requested = [_parse_task_id(value) for value in args.ids]
            duplicates = sorted({
                task_id for task_id in requested
                if requested.count(task_id) > 1
            })
            if duplicates:
                raise tasks.TaskError(
                    "task " + ", ".join("t" + str(i) for i in duplicates)
                    + " is listed more than once"
                )
        root = _project_root()
        metadata = load_project_metadata(root)
        reviewer_model = _reviewer_model(metadata)
        escalation_worker_model = _escalation_worker_model(metadata)
        validation_commands = validation.commands_from_metadata(metadata)
        base_branch = _run_base_branch(root, metadata)
        binary = opencode.find_binary()
        actl_dir = root / PROJECT_DIR
        _preflight_tasks(actl_dir, requested)
    except (
        ProjectError, tasks.TaskError, execution.ExecutionError,
        opencode.OpenCodeError, validation.ValidationError,
    ) as exc:
        return _fail(str(exc))
    try:
        with execution.lock(actl_dir) as lock_fd:
            # An interrupted execution leaves its run RUNNING; the batch
            # resumes that run instead of opening a second one, so staged
            # tasks stay in the same run and keep satisfying their
            # dependents (only one run executes at a time).
            run = _running_run(actl_dir)
            if run is None:
                # Only one unfinished, unmerged run may exist: a completed
                # run that still holds staged tasks must be integrated before
                # a new run starts (a run stopped before staging anything
                # does not block). Dependency problems in the requested tasks
                # are reported first, with the exact command that fixes them.
                blocker = _staged_unmerged_run(actl_dir)
                if blocker is not None:
                    return _fail(
                        _new_run_refusal(actl_dir, blocker, requested)
                    )
            if requested is None:
                return _run_all(
                    binary, root, actl_dir, lock_fd, metadata, base_branch,
                    reviewer_model, escalation_worker_model,
                    validation_commands, run,
                )
            return _run_explicit(
                binary, root, actl_dir, lock_fd, requested, metadata,
                base_branch, reviewer_model, escalation_worker_model,
                validation_commands, run,
            )
    except (
        ProjectError, tasks.TaskError, sqlite3.Error,
        execution.ExecutionError, opencode.OpenCodeError,
        validation.ValidationError, review.ReviewError,
    ) as exc:
        return _fail(str(exc))


def _preflight_tasks(actl_dir: Path, requested) -> None:
    """Validate the requested tasks before the execution lock is taken.

    Explicit ids must exist, so a typo is reported before anything runs or
    changes. ``--all`` (``requested`` is None) refuses an unexpected status
    outright instead of silently skipping a task it cannot classify.
    """
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        if requested is not None:
            for task_id in requested:
                tasks.get_task(conn, task_id)
            return
        for task in tasks.list_tasks(conn):
            if task.status in _RUNNABLE_STATUSES:
                continue
            if task.status in (
                tasks.STATUS_DONE, tasks.STATUS_NEEDS_HUMAN, tasks.STATUS_STAGED,
            ):
                continue
            raise tasks.TaskError(
                "cannot run --all: task t" + str(task.id)
                + " has unexpected status " + repr(task.status)
            )
    finally:
        conn.close()


def _run_explicit(
    binary, root, actl_dir, lock_fd, requested, metadata, base_branch,
    reviewer_model, escalation_worker_model, validation_commands, run,
) -> int:
    """Execute an explicit task list strictly in the order it was given.

    Dependencies are checked for each task when its turn comes, so a task may
    depend on an earlier task of the same list: that one has become STAGED in
    the same run by then. The list is never reordered and execution stops at
    the first task that cannot complete; the run keeps the staged tasks.
    """
    batch = len(requested) > 1
    completed = []
    for task_id in requested:
        code, detail, run = _execute_task(
            binary, root, actl_dir, task_id, metadata, lock_fd, base_branch,
            reviewer_model, escalation_worker_model, validation_commands, run,
        )
        if code != 0:
            if batch:
                _print_summary(completed, stopped=(task_id, detail))
            _finish_run(actl_dir, root, run)
            return code
        completed.append(task_id)
    if batch:
        _print_summary(completed)
    _finish_run(actl_dir, root, run)
    return 0


def _dependency_satisfied(dep_id, state, run) -> bool:
    """Whether a dependency unlocks its dependent in the current run.

    DONE always satisfies. STAGED satisfies only when it belongs to the run
    the dependent is currently executing in: work staged in another run is
    not part of this run branch and must first be marked DONE by
    ``actl run merge``.
    """
    status, dep_run = state.get(dep_id, ("missing", None))
    if status == tasks.STATUS_DONE:
        return True
    return status == tasks.STATUS_STAGED and run is not None and dep_run == run.id


def _run_all(
    binary, root, actl_dir, lock_fd, metadata, base_branch,
    reviewer_model, escalation_worker_model, validation_commands, run,
) -> int:
    """Execute every eligible task in dependency order, one at a time.

    Selection is deterministic: at every step the eligible task with the
    smallest id whose dependencies are satisfied runs first, and the task
    list is re-read after each task so a staged task immediately unlocks its
    dependents in the same run. A task whose dependencies stay unfinished
    can never run; when no task is ready the batch reports what blocks
    progress and stops.
    """
    completed = []
    while True:
        all_tasks = _list_tasks(actl_dir)
        state = {task.id: (task.status, task.run_id) for task in all_tasks}
        candidates = [
            task for task in all_tasks if task.status in _RUNNABLE_STATUSES
        ]
        if not candidates:
            break
        ready = next(
            (
                task for task in candidates
                if all(
                    _dependency_satisfied(dep, state, run)
                    for dep in task.depends_on
                )
            ),
            None,
        )
        if ready is None:
            blocked = []
            staged = None
            for task in candidates:
                unfinished = [
                    dep for dep in task.depends_on
                    if not _dependency_satisfied(dep, state, run)
                ]
                if not unfinished:
                    continue
                blocked.append(
                    "t" + str(task.id) + " waits for "
                    + ", ".join(
                        "t" + str(dep) + " ("
                        + state.get(dep, ("missing", None))[0] + ")"
                        for dep in unfinished
                    )
                )
                if staged is None:
                    for dep in unfinished:
                        status, dep_run = state.get(dep, ("missing", None))
                        if status == tasks.STATUS_STAGED:
                            staged = _dependency_staged_message(
                                task.id, dep, dep_run
                            )
            _print_summary(completed, blocked=blocked)
            _finish_run(actl_dir, root, run)
            if staged is not None:
                return _fail(staged)
            return _fail(
                "progress is blocked: " + "; ".join(blocked)
                + "; resolve the unfinished dependencies and run 'actl task"
                " run --all' again"
            )
        code, detail, run = _execute_task(
            binary, root, actl_dir, ready.id, metadata, lock_fd, base_branch,
            reviewer_model, escalation_worker_model, validation_commands, run,
        )
        if code != 0:
            _print_summary(completed, stopped=(ready.id, detail))
            _finish_run(actl_dir, root, run)
            return code
        completed.append(ready.id)
    skipped = [
        task.id for task in _list_tasks(actl_dir)
        if task.status == tasks.STATUS_NEEDS_HUMAN
    ]
    _print_summary(completed, needs_human=skipped)
    _finish_run(actl_dir, root, run)
    return 0


def _finish_run(actl_dir: Path, root: Path, run) -> None:
    """Record the run's final commit and mark it COMPLETE.

    Called whenever a batch stops (successfully or at a failed task), so the
    run branch is frozen at a known commit that ``actl run merge`` verifies.
    Interruptions and crashes never reach this point: the run stays RUNNING
    and the next ``actl task run`` resumes it.
    """
    if run is None:
        return
    try:
        final = execution.ref_commit(root, "refs/heads/" + run.branch)
    except execution.ExecutionError as exc:
        _event(
            "Warning: cannot record the final commit of run r" + str(run.id)
            + " (" + str(exc) + ")"
        )
        return
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        tasks.complete_run(conn, run.id, final)
        staged = [
            task.id for task in tasks.run_tasks(conn, run.id)
            if task.status == tasks.STATUS_STAGED
        ]
    finally:
        conn.close()
    if staged:
        detail = (
            str(len(staged)) + " staged task"
            + ("s" if len(staged) != 1 else "")
        )
    else:
        detail = "no staged tasks"
    _event(
        "Run r" + str(run.id) + " finished with " + detail
        + "; integrate it with 'actl run merge r" + str(run.id) + "'"
    )


def _print_summary(
    completed, stopped=None, blocked=None, needs_human=(),
) -> None:
    """Print the final summary of a multi-task execution."""
    lines = [
        "completed: " + (
            ", ".join("t" + str(task_id) for task_id in completed)
            if completed else "none"
        )
    ]
    if stopped is not None:
        task_id, detail = stopped
        lines.append("stopped at t" + str(task_id) + ": " + detail)
    if blocked:
        lines.append("blocked: " + "; ".join(blocked))
    if needs_human:
        lines.append(
            "needs human intervention: "
            + ", ".join("t" + str(task_id) for task_id in needs_human)
        )
    print("")
    print("Summary: " + "; ".join(lines))


def _execute_task(
    binary, root, actl_dir, task_id, metadata, lock_fd, base_branch,
    reviewer_model, escalation_worker_model, validation_commands, run,
) -> tuple:
    """Run one task while the execution lock is held.

    Every task executes inside a run: the batch's current run is reused when
    one is running (an interrupted execution) and a new run is created lazily
    before the first task that actually executes. The task branch is created
    from the run branch, and an approval is fast-forwarded into the run
    branch and marked STAGED - never into the configured base branch.

    A task gets up to three completed worker attempts in the same branch and
    worktree: the first two with the project's worker model and the third
    with the project's escalation model. A failed validation command or a
    reviewer rejection with critical or major findings feeds the next
    attempt; the attempt count and the feedback are persisted per task so
    restarting actl never resets the budget. An interrupted attempt does not
    count: a retry resumes the same attempt number. Exhausting the attempts
    marks the task NEEDS_HUMAN.

    Only implementation failures are repaired. A worker or reviewer
    execution error stops the run with FAILED: infrastructure problems never
    launch another repair worker, and a retry after such a failure validates
    and reviews the committed implementation again instead of spending
    another worker attempt.

    An APPROVED task skips the worker, validation, and review and only
    attempts to stage its approved commit. Returns ``(exit code, short
    reason, run)``: ``(0, "")`` exactly when the task was successfully staged
    (or was already staged in this run); the short reason describes the stop
    for a multi-task summary, while the full error is printed here or in the
    helper that returned it.

    The terminal shows only actl's own timestamped events; transcripts and
    validation output go straight into their log files while they run.
    """
    try:
        task = _load_task(actl_dir, task_id)
    except tasks.TaskError as exc:
        _fail(str(exc))
        return 1, str(exc), run

    if task.status == tasks.STATUS_STAGED:
        if run is not None and task.run_id == run.id:
            _event(
                "Task t" + str(task.id) + " is already STAGED in run r"
                + str(run.id)
            )
            return 0, "", run
        _fail(_staged_block_message(task))
        return 1, "already staged", run

    try:
        _require_runnable(task, actl_dir, run)
    except tasks.TaskError as exc:
        _fail(str(exc))
        return 1, str(exc), run

    if run is None:
        try:
            run = _create_run(actl_dir, root, base_branch)
        except (execution.ExecutionError, tasks.TaskError, sqlite3.Error) as exc:
            _fail("cannot start a run: " + str(exc))
            return 1, "cannot start a run", None
    try:
        worktree = execution.ensure_run_worktree(root, actl_dir, run)
    except execution.ExecutionError as exc:
        _fail("cannot prepare run r" + str(run.id) + ": " + str(exc))
        return 1, "cannot prepare the run", run
    if worktree.created:
        _event(
            "Created run r" + str(run.id) + " (branch " + run.branch
            + ", worktree " + str(worktree.path) + ")"
        )

    if task.status == tasks.STATUS_APPROVED:
        # The implementation was validated and approved by an earlier run
        # (or an earlier actl version): stage the exact approved commit
        # without rerunning the worker, validation, or review. The repair
        # budget is irrelevant here.
        print("task t" + str(task.id) + ": " + task.title)
        try:
            approved = _approved_commit(actl_dir, task)
        except tasks.TaskError as exc:
            _fail(str(exc))
            return 1, str(exc), run
        return _complete_task(actl_dir, root, task, approved, run)

    try:
        attempts, feedback = _attempt_baseline(actl_dir, task)
    except execution.ExecutionError as exc:
        # A corrupt or unreadable attempt record is an infrastructure
        # failure: fail the task instead of silently resetting its budget.
        return _verification_failed(actl_dir, task.id, str(exc), run)

    print("task t" + str(task.id) + ": " + task.title)
    # Validate and review the existing committed implementation first when
    # one is pending: an IMPLEMENTED task was interrupted between the worker
    # and validation/review, and a FAILED task whose last worker attempt
    # completed without pending feedback failed in validation or review
    # infrastructure. Neither case may spend another worker attempt.
    review_first = not feedback and (
        task.status == tasks.STATUS_IMPLEMENTED
        or (task.status == tasks.STATUS_FAILED and attempts > 0)
    )
    if review_first:
        print("reviewer model: " + reviewer_model)
        if task.status == tasks.STATUS_FAILED:
            _event(
                "Re-verifying task t" + str(task.id)
                + ", left FAILED by an infrastructure failure"
            )
    else:
        print("model: " + _worker_model(metadata))
        print("reviewer model: " + reviewer_model)
        print("escalation model: " + escalation_worker_model)
        if task.status == tasks.STATUS_RUNNING:
            _event(
                "Retrying task t" + str(task.id)
                + ", left RUNNING by an interrupted execution"
            )

    if review_first:
        verification = _validate_and_review(
            binary, root, actl_dir, task, run.branch, reviewer_model,
            validation_commands, lock_fd, attempt=max(attempts, 1),
            announce_worktree=True,
        )
        if verification.outcome == "approved":
            return _complete_task(
                actl_dir, root, task, verification.commit, run,
            )
        if verification.outcome == "failed":
            return _verification_failed(
                actl_dir, task.id, verification.reason, run,
                log_hint=verification.log_hint,
            )
        # The existing implementation spends one worker attempt: it was
        # produced by an earlier worker run, even if none was recorded.
        attempts = max(attempts, 1)
        feedback = verification.feedback
        execution.record_attempt_state(actl_dir, task.id, attempts, feedback)
        if attempts >= execution.MAX_WORKER_ATTEMPTS:
            return _needs_human(actl_dir, task, verification, attempts, run)
        _set_task_status(actl_dir, task.id, tasks.STATUS_RUNNING)

    while attempts < execution.MAX_WORKER_ATTEMPTS:
        attempt = attempts + 1
        model = _attempt_model(metadata, attempt, escalation_worker_model)
        _event(
            "Worker attempt " + str(attempt) + "/"
            + str(execution.MAX_WORKER_ATTEMPTS) + " (" + model + ")"
        )
        status, reason, log_path = _run_worker_attempt(
            binary, root, actl_dir, task, lock_fd, run.branch,
            attempt=attempt, model=model, feedback=feedback,
            retry=not (attempt == 1 and task.status == tasks.STATUS_PENDING),
            announce_worktree=attempt == 1,
        )
        if status != tasks.STATUS_IMPLEMENTED:
            _set_task_status(actl_dir, task.id, status)
            _event("Task status: " + status)
            message = (
                "task t" + str(task.id) + " failed: " + reason
                + " (log: " + str(log_path) + ")"
            )
            _fail(message)
            return 1, "worker failed", run
        attempts = attempt
        # The attempt counts as used only now that it completed; an
        # interrupted attempt is retried under the same number and does not
        # consume the budget. The feedback was handed to the worker that just
        # ran, so clear it: an interruption during validation or review
        # re-verifies this commit instead of launching another worker.
        execution.record_attempt_state(actl_dir, task.id, attempts, "")
        _set_task_status(actl_dir, task.id, tasks.STATUS_IMPLEMENTED)
        _event("Task status: IMPLEMENTED")

        verification = _validate_and_review(
            binary, root, actl_dir, task, run.branch, reviewer_model,
            validation_commands, lock_fd, attempt=attempt,
        )
        if verification.outcome == "approved":
            return _complete_task(
                actl_dir, root, task, verification.commit, run,
            )
        if verification.outcome == "failed":
            return _verification_failed(
                actl_dir, task.id, verification.reason, run,
                log_hint=verification.log_hint,
            )
        feedback = verification.feedback
        execution.record_attempt_state(actl_dir, task.id, attempts, feedback)
        if attempts >= execution.MAX_WORKER_ATTEMPTS:
            return _needs_human(actl_dir, task, verification, attempts, run)
        _set_task_status(actl_dir, task.id, tasks.STATUS_RUNNING)

    # The budget was already exhausted by an earlier attempt (for example a
    # reviewer execution error after the third worker attempt).
    return _needs_human(actl_dir, task, None, attempts, run)


def _run_worker_attempt(
    binary, root, actl_dir, task, lock_fd, base_branch, attempt, model,
    feedback, retry, announce_worktree,
):
    """Run one worker attempt in the task worktree.

    Returns ``(status, reason, log_path)``: ``IMPLEMENTED`` with an empty
    reason when the worker left a clean, committed implementation;
    ``FAILED`` with a reason for a worker or OpenCode execution error. The
    attempt continues on the existing branch and never resets it.
    """
    log_path, log = execution.open_run_log(actl_dir, task.id)
    with log:
        log.write("actl task run\n")
        log.write("task: t" + str(task.id) + " - " + task.title + "\n")
        log.write(
            "attempt: " + str(attempt) + " of "
            + str(execution.MAX_WORKER_ATTEMPTS) + "\n"
        )
        log.write("model: " + model + "\n")
        log.write("started_at: " + _utc_now() + "\n")

        _set_task_status(actl_dir, task.id, tasks.STATUS_RUNNING)
        status = tasks.STATUS_FAILED
        reason = ""
        exit_code = None
        try:
            if announce_worktree:
                _event("Preparing worktree")
            worktree = execution.ensure_worktree(
                root, actl_dir, task, retry=retry, base_branch=base_branch,
            )
            log.write("branch: " + worktree.branch + "\n")
            log.write("worktree: " + str(worktree.path) + "\n")
            if announce_worktree:
                if worktree.created:
                    _event(
                        "Created worktree " + str(worktree.path)
                        + " (branch " + worktree.branch + ")"
                    )
                else:
                    _event(
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
            _event("Starting worker (session " + session.id + ")")
            _event("log: " + str(log_path))
            exit_code = opencode.run_headless(
                binary, worktree.path, session.id, model,
                execution.worker_prompt(
                    task, worktree, base_branch, attempt=attempt,
                    total=execution.MAX_WORKER_ATTEMPTS, feedback=feedback,
                ),
                log, pass_fds=(lock_fd,),
            )
            if exit_code == 0:
                _event("Worker finished with exit code 0")
            else:
                _event("Worker failed with exit code " + str(exit_code))
            status, reason = _evaluate_run(worktree, exit_code, base_branch)
        except (execution.ExecutionError, opencode.OpenCodeError, OSError) as exc:
            reason = str(exc)

        log.write("---\n")
        log.write("finished_at: " + _utc_now() + "\n")
        if exit_code is not None:
            log.write("exit_code: " + str(exit_code) + "\n")
        log.write("result: " + status + "\n")
        if reason:
            log.write("reason: " + reason + "\n")
    return status, reason, log_path


@dataclass(frozen=True)
class _Verification:
    """The outcome of validating and reviewing one implementation commit."""

    outcome: str  # "approved", "rejected", "validation_failed", "failed"
    reason: str = ""
    feedback: str = ""
    findings: tuple = ()
    verdict: object = None
    log_hint: object = None
    commit: str = ""


def _validate_and_review(
    binary, root, actl_dir, task, base_branch, reviewer_model,
    validation_commands, lock_fd, attempt, announce_worktree=False,
) -> _Verification:
    """Validate and review one implemented commit.

    Validation and review must refer to one exact commit: it is recorded
    before validation, the task worktree must stay clean and on its branch,
    and the task branch must still point at that commit before any verdict is
    recorded.

    A failed validation command and a reviewer rejection with critical or
    major findings are implementation outcomes the caller repairs; the
    returned feedback explains them to the next worker. A validation
    integrity failure, a reviewer execution or format failure, and any other
    orchestration error return ``"failed"`` and never trigger a repair
    worker.
    """
    try:
        worktree = execution.ensure_worktree(
            root, actl_dir, task, retry=True, base_branch=base_branch,
        )
        if announce_worktree:
            if worktree.created:
                _event(
                    "Created worktree " + str(worktree.path)
                    + " (branch " + worktree.branch + ")"
                )
            else:
                _event(
                    "Reusing worktree " + str(worktree.path)
                    + " (branch " + worktree.branch + ")"
                )
        commit = _verification_commit(root, worktree, base_branch)
    except (execution.ExecutionError, opencode.OpenCodeError, OSError) as exc:
        return _Verification("failed", reason=str(exc))

    validation_result = None
    validation_log_path = None
    if validation_commands:
        validation_log_path, log = execution.open_stage_log(
            actl_dir, task.id, "validation",
        )
        _event("Validation started (" + str(len(validation_commands)) + " commands)")
        _event("validation log: " + str(validation_log_path))
        try:
            with log:
                log.write("actl validation\n")
                log.write("task: t" + str(task.id) + " - " + task.title + "\n")
                log.write("attempt: " + str(attempt) + "\n")
                log.write("branch: " + worktree.branch + "\n")
                log.write("commit: " + commit + "\n")
                log.write("started_at: " + _utc_now() + "\n---\n")
                try:
                    validation_result = validation.run(
                        validation_commands, worktree.path, log,
                        verify=_validation_integrity(root, worktree, commit),
                    )
                except OSError as exc:
                    log.write(
                        "result: FAILED\nreason: validation could not run: "
                        + str(exc) + "\n"
                    )
                    raise
                log.write("---\nfinished_at: " + _utc_now() + "\n")
                log.write(
                    "result: "
                    + ("PASSED" if validation_result.passed else "FAILED") + "\n"
                )
                if validation_result.failed:
                    log.write("failed_command: " + validation_result.failed + "\n")
                elif validation_result.reason:
                    log.write("reason: " + validation_result.reason + "\n")
        except OSError as exc:
            return _Verification(
                "failed",
                reason="validation could not run: " + str(exc),
                log_hint=validation_log_path,
            )
        if not validation_result.passed:
            if validation_result.failed:
                exit_code = validation_result.results[-1][1]
                _event(
                    "Validation failed (" + validation_result.failed
                    + ", exit code " + str(exit_code) + ")"
                )
                return _Verification(
                    "validation_failed",
                    reason="validation failed: " + validation_result.failed
                    + " exited with status " + str(exit_code),
                    feedback=_validation_feedback(
                        attempt, validation_result, validation_log_path
                    ),
                    log_hint=validation_log_path,
                )
            # A validation command that moved HEAD or changed tracked files is
            # a project or validation-configuration problem, not something a
            # worker repair could address: fail without a repair attempt.
            _event("Validation failed (worktree or commit changed)")
            return _Verification(
                "failed",
                reason="validation failed: " + validation_result.reason,
                log_hint=validation_log_path,
            )
        _event("Validation passed")
    else:
        _event("Validation skipped (no validation commands configured)")

    review_log_path, review_log = execution.open_stage_log(
        actl_dir, task.id, "review",
    )
    _event("Reviewer started (model " + reviewer_model + ")")
    _event("review log: " + str(review_log_path))
    try:
        try:
            verdict = _run_reviewer(
                binary, root, actl_dir, task, worktree, commit, base_branch,
                reviewer_model, validation_result, lock_fd, review_log,
                attempt,
            )
        finally:
            review_log.close()
    except (
        execution.ExecutionError, opencode.OpenCodeError, review.ReviewError,
        OSError,
    ) as exc:
        _event("Review failed")
        return _Verification(
            "failed",
            reason="review failed: " + str(exc),
            log_hint=review_log_path,
        )

    try:
        _require_commit_intact(root, worktree, commit, "the review")
    except execution.ExecutionError as exc:
        return _Verification(
            "failed", reason=str(exc), log_hint=review_log_path,
        )

    _record_review(actl_dir, task.id, commit, verdict)
    if verdict.decision == review.DECISION_APPROVED:
        _event(
            "Review approved ("
            + (_finding_summary(verdict.findings) or "no findings") + ")"
        )
        _print_findings(verdict.findings)
        return _Verification(
            "approved", verdict=verdict, findings=verdict.findings,
            log_hint=review_log_path, commit=commit,
        )

    _event("Review requested changes (" + _finding_summary(verdict.blocking) + ")")
    _print_findings(verdict.findings)
    return _Verification(
        "rejected",
        reason="review requested changes: " + _finding_summary(verdict.blocking),
        feedback=_review_feedback(attempt, verdict.blocking),
        verdict=verdict,
        findings=verdict.findings,
        log_hint=review_log_path,
    )


def _run_reviewer(
    binary, root, actl_dir, task, worktree, commit, base_branch,
    reviewer_model, validation_result, lock_fd, log, attempt=None,
) -> review.Verdict:
    """Run the reviewer in a temporary worktree at ``commit``.

    The worktree is discarded on every path. The returned verdict has already
    been validated; malformed output raises :class:`review.ReviewError`. The
    reviewer always evaluates the complete cumulative task diff, never just
    the latest repair commit.
    """
    base_commit = execution.branch_base(actl_dir, task.id)
    if base_commit is None:
        base_commit = execution.merge_base(
            root, "refs/heads/" + base_branch, commit,
        )
    diff = execution.commit_diff(root, base_commit, commit)
    policy = review.policy_text(review.load_project_policy(root))
    prompt = review.build_prompt(
        task, base_commit, commit, diff,
        validation.summary(validation_result), policy,
    )

    review_path = (
        actl_dir / execution.REVIEW_DIR
        / ("t" + str(task.id) + "-" + execution.timestamp())
    )
    execution.add_review_worktree(root, review_path, commit)
    try:
        log.write("actl review\n")
        log.write("task: t" + str(task.id) + " - " + task.title + "\n")
        if attempt is not None:
            log.write("attempt: " + str(attempt) + "\n")
        log.write("model: " + reviewer_model + "\n")
        log.write("commit: " + commit + "\n")
        log.write("base_commit: " + base_commit + "\n")
        log.write("review_worktree: " + str(review_path) + "\n")
        log.write("started_at: " + _utc_now() + "\n---\n")
        session = opencode.create_session(
            binary, review_path,
            title="actl review t" + str(task.id) + ": " + task.title,
            agent=opencode.REVIEWER_AGENT,
        )
        log.write("session: " + session.id + "\n")
        # Flush before the reviewer starts writing to the same file through
        # its inherited stdout descriptor, so the header stays on top.
        log.flush()
        try:
            exit_code = opencode.run_headless(
                binary, review_path, session.id, reviewer_model, prompt, log,
                pass_fds=(lock_fd,), agent=opencode.REVIEWER_AGENT,
                output_format="json",
            )
        except (opencode.OpenCodeError, OSError) as exc:
            _log_review_failure(log, str(exc))
            raise
        log.write("---\nfinished_at: " + _utc_now() + "\n")
        log.write("exit_code: " + str(exit_code) + "\n")
        log.flush()
        if exit_code != 0:
            _log_review_failure(
                log, "the reviewer exited with status " + str(exit_code)
            )
            raise review.ReviewError(
                "the reviewer exited with status " + str(exit_code)
            )
        try:
            _require_reviewer_checkout(review_path, commit)
        except (review.ReviewError, execution.ExecutionError) as exc:
            _log_review_failure(log, str(exc))
            raise
        log.write("checkout: " + commit + " (unchanged)\n")
        log.flush()
        output = Path(log.name).read_text(encoding="utf-8", errors="replace")
        try:
            verdict = review.verdict_from_output(output)
        except review.ReviewError as exc:
            _log_review_failure(log, str(exc))
            raise
        log.write("decision: " + verdict.decision + "\n")
        log.write(
            "findings: "
            + json.dumps([finding.as_dict() for finding in verdict.findings])
            + "\n"
        )
        return verdict
    finally:
        problem = execution.remove_temporary_worktree(root, review_path)
        if problem:
            _event(
                "Warning: could not remove the reviewer worktree "
                + str(review_path) + " (" + problem + ")"
            )


def _verification_commit(root: Path, worktree, base_branch: str) -> str:
    """Return the commit to validate, requiring a clean worktree on its branch.

    The branch must still contain committed changes relative to the base: a
    branch deleted and silently recreated from the base would otherwise be
    validated and reviewed as an empty implementation.
    """
    branch = execution.worktree_branch(worktree.path)
    if branch != worktree.branch:
        raise execution.ExecutionError(
            "the task worktree is on '" + branch + "' instead of '"
            + worktree.branch + "'"
        )
    dirty = execution.worktree_tracked_dirty(worktree.path)
    if dirty:
        raise execution.ExecutionError(
            "the task worktree has uncommitted changes; commit or remove them"
            " before validation"
        )
    if not execution.worktree_has_committed_changes(worktree.path, base_branch):
        raise execution.ExecutionError(
            "the task branch has no committed changes relative to "
            + base_branch + "; refusing to validate or review it"
        )
    head = execution.head_commit(worktree.path)
    if execution.ref_commit(root, "refs/heads/" + worktree.branch) != head:
        raise execution.ExecutionError(
            "branch " + worktree.branch + " does not point at the worktree"
            " commit"
        )
    return head


def _require_commit_intact(root: Path, worktree, commit: str, stage: str) -> None:
    """Refuse approval when ``stage`` changed the commit under review."""
    head = execution.head_commit(worktree.path)
    ref = execution.ref_commit(root, "refs/heads/" + worktree.branch)
    if head != commit or ref != commit:
        raise execution.ExecutionError(
            stage + " changed the task commit: expected " + commit[:12]
            + ", found " + head[:12] + " in the worktree and " + ref[:12]
            + " on " + worktree.branch + "; refusing approval"
        )
    if execution.worktree_tracked_dirty(worktree.path):
        raise execution.ExecutionError(
            stage + " modified tracked files in the task worktree; refusing"
            " approval"
        )


def _validation_integrity(root: Path, worktree, commit: str):
    """Return the validation check that runs after every command.

    Validation commands run inside the task worktree, so a command that
    modifies tracked files or moves HEAD must fail the task immediately; a
    later command could otherwise validate the modified state and restore the
    original before a final check.
    """
    def verify() -> str:
        try:
            _require_commit_intact(root, worktree, commit, "a validation command")
        except execution.ExecutionError as exc:
            return str(exc)
        return ""

    return verify


def _require_reviewer_checkout(review_path: Path, commit: str) -> None:
    """Refuse a verdict when the reviewer changed its own checkout.

    The reviewer must evaluate the validated commit; a checkout that moved or
    carries modified tracked files no longer represents it. Untracked scratch
    files are harmless and allowed.
    """
    head = execution.head_commit(review_path)
    if head != commit:
        raise review.ReviewError(
            "the reviewer changed its checkout: expected " + commit[:12]
            + ", found " + head[:12] + "; refusing approval"
        )
    if execution.worktree_tracked_dirty(review_path):
        raise review.ReviewError(
            "the reviewer modified tracked files in its checkout; refusing"
            " approval"
        )


def _log_review_failure(log, reason: str) -> None:
    """Record a review failure in the review log; it is never an approval."""
    log.write("result: FAILED\nreason: " + reason + "\n")
    log.flush()


def _verification_failed(
    actl_dir, task_id, reason, run, log_hint=None,
) -> tuple:
    """Mark a task FAILED after validation, review, or state loading failed."""
    _set_task_status(actl_dir, task_id, tasks.STATUS_FAILED)
    print("")
    print("Task t" + str(task_id) + " \u2192 FAILED")
    message = "task t" + str(task_id) + " failed: " + reason
    if log_hint is not None:
        message += " (log: " + str(log_hint) + ")"
    _fail(message)
    return 1, "verification failed", run


def _record_review(actl_dir: Path, task_id: int, commit: str, verdict) -> None:
    """Store the verdict (and its findings) for later stages."""
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        tasks.record_review(
            conn, task_id, commit, verdict.decision,
            [finding.as_dict() for finding in verdict.findings],
        )
    finally:
        conn.close()


def _attempt_baseline(actl_dir: Path, task):
    """Return the task's recorded ``(attempts, feedback)`` for this run.

    A Stage 5 task that ended NEEDS_CHANGES continues from its remaining
    attempts: its first worker attempt and the review rejection are already
    history, so the next run starts at attempt 2 with the stored blocking
    findings as feedback. When no review row exists the attempt is still
    counted - the status itself proves that a rejection happened - but no
    feedback can be reconstructed.
    """
    state = execution.attempt_state(actl_dir, task.id)
    if state is not None:
        return state
    if task.status == tasks.STATUS_NEEDS_CHANGES:
        feedback = _legacy_review_feedback(actl_dir, task.id)
        # Persist the derived baseline immediately: a later worker execution
        # error changes the status to FAILED, and the spent attempt must not
        # be forgotten when the task is retried.
        execution.record_attempt_state(actl_dir, task.id, 1, feedback)
        return 1, feedback
    return 0, ""


def _attempt_model(
    metadata: dict, attempt: int, escalation_worker_model: str,
) -> str:
    """Return the model for worker ``attempt`` (1-based).

    The first two attempts use the project's worker model; the third and
    final attempt uses the configured escalation worker model.
    """
    if attempt >= execution.MAX_WORKER_ATTEMPTS:
        return escalation_worker_model
    return _worker_model(metadata)


def _legacy_review_feedback(actl_dir: Path, task_id: int) -> str:
    """Rebuild repair feedback from the newest stored review, if any."""
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        stored = tasks.latest_review(conn, task_id)
    finally:
        conn.close()
    if not stored or stored.get("decision") != tasks.REVIEW_CHANGES_REQUESTED:
        return ""
    findings = tuple(
        review.Finding(
            severity=item.get("severity", ""),
            description=item.get("description", ""),
            scenario=item.get("scenario", ""),
            impact=item.get("impact", ""),
            location=item.get("location", ""),
        )
        for item in stored.get("findings", [])
        if isinstance(item, dict)
        and item.get("severity") in review.BLOCKING_SEVERITIES
    )
    if not findings:
        return ""
    return _review_feedback(1, findings)


def _validation_feedback(attempt: int, result, log_path) -> str:
    """Describe a failed validation command for the next worker attempt."""
    lines = [
        "The previous implementation (worker attempt " + str(attempt)
        + ") failed the project's validation:",
        "",
        "Failed command: " + result.failed,
    ]
    if result.results:
        lines.append("Exit code: " + str(result.results[-1][1]))
    lines += [
        "",
        "Relevant validation output (tail of " + str(log_path) + "):",
        _file_tail(log_path),
    ]
    return "\n".join(lines)


def _review_feedback(attempt: int, findings) -> str:
    """Describe blocking reviewer findings for the next worker attempt."""
    lines = [
        "The independent reviewer rejected the previous implementation"
        " (worker attempt " + str(attempt) + ") with these blocking findings:",
        "",
    ]
    for finding in findings:
        lines.append("- [" + finding.severity + "] " + finding.description)
        if finding.scenario:
            lines.append("  scenario: " + finding.scenario)
        if finding.impact:
            lines.append("  impact: " + finding.impact)
        if finding.location:
            lines.append("  location: " + finding.location)
    return "\n".join(lines)


def _file_tail(path, limit: int = 4000) -> str:
    """Return the end of a log file, for worker feedback."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(the log could not be read)"
    if len(text) > limit:
        return "...\n" + text[-limit:]
    return text.rstrip()


def _approved_commit(actl_dir: Path, task) -> str:
    """Return the commit the task's stored approval refers to, or raise.

    The approved commit comes from the recorded reviewer verdict, so staging
    merges exactly the commit that was validated and approved - never
    whatever the task branch happens to point at now. A task marked APPROVED
    without a recorded approval is an inconsistent state and is reported
    instead of staged.
    """
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        stored = tasks.latest_review(conn, task.id)
    finally:
        conn.close()
    if (
        stored is None
        or stored.get("decision") != tasks.REVIEW_APPROVED
        or not stored.get("commit_sha")
    ):
        raise tasks.TaskError(
            "cannot integrate task t" + str(task.id) + ": no approved review"
            " is recorded for it, so its approved commit cannot be verified;"
            " inspect the task branch and .actl/tasks.db and intervene by hand"
        )
    return stored["commit_sha"]


def _complete_task(actl_dir, root, task, commit, run) -> tuple:
    """Persist APPROVED, fast-forward into the run branch, then mark STAGED.

    The approval is recorded before Git is touched: when staging is
    impossible (a dirty run worktree, a non-fast-forward merge, a moved task
    branch) the task keeps its APPROVED state and an explicit rerun stages
    the same approved commit again without rerunning the worker, validation,
    or review. STAGED is recorded only after the merge succeeded, so a
    failure between the two Git/SQLite steps leaves the exact approved commit
    ready for a safe repeat.

    The base branch is never touched here: only ``actl run merge`` integrates
    a run branch into the configured base branch.
    """
    _set_task_status(actl_dir, task.id, tasks.STATUS_APPROVED)
    _event("Fast-forward merging into run r" + str(run.id) + "...")
    try:
        execution.integrate_commit(
            root, actl_dir, run.branch, execution.branch_name(task.id),
            commit,
        )
    except execution.ExecutionError as exc:
        print("")
        print("Task t" + str(task.id) + " \u2192 APPROVED (not staged)")
        _fail(
            "task t" + str(task.id) + " staging failed: " + str(exc)
            + " (the task keeps its APPROVED state)"
        )
        return 1, "staging failed", run
    try:
        _stage_task(actl_dir, task.id, run.id)
    except (tasks.TaskError, sqlite3.Error) as exc:
        # The commit is already in the run branch; a rerun detects that and
        # records STAGED without rerunning the worker, validation, or review.
        print("")
        print("Task t" + str(task.id) + " \u2192 APPROVED (not staged)")
        _fail(
            "task t" + str(task.id) + " was merged into run r" + str(run.id)
            + " but could not be marked STAGED (" + str(exc)
            + "); run the task again to record it"
        )
        return 1, "staging status failed", run
    print("")
    print("Task t" + str(task.id) + " \u2192 STAGED (run r" + str(run.id) + ")")
    return 0, "", run


def _needs_human(actl_dir, task, verification, attempts, run) -> tuple:
    """Mark the task NEEDS_HUMAN after its three attempts are exhausted."""
    _set_task_status(actl_dir, task.id, tasks.STATUS_NEEDS_HUMAN)
    print("")
    print("Task t" + str(task.id) + " \u2192 NEEDS_HUMAN")
    if verification is not None and verification.outcome == "rejected":
        blocking = (
            verification.verdict.blocking
            if verification.verdict is not None else verification.findings
        )
        detail = (
            "the reviewer still reports blocking findings after "
            + str(attempts) + " worker attempts ("
            + _finding_summary(blocking) + ")"
        )
    elif verification is not None and verification.reason:
        detail = (
            verification.reason + " after " + str(attempts)
            + " worker attempts"
        )
    else:
        detail = (
            "all " + str(attempts) + " worker attempts were used and the"
            " implementation is still not approved"
        )
    message = "task t" + str(task.id) + " needs human intervention: " + detail
    if verification is not None and verification.log_hint is not None:
        message += " (log: " + str(verification.log_hint) + ")"
    message += (
        "; inspect the task branch, worktree, and logs and intervene by hand"
        " (removing .actl/attempts/t" + str(task.id) + " grants a deliberate"
        " fresh three-attempt budget)"
    )
    _fail(message)
    return 1, "needs human intervention", run


def _finding_summary(findings) -> str:
    """Describe findings, for example ``"2 minor findings"``."""
    if not findings:
        return ""
    counts = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    parts = [
        str(counts[severity]) + " " + severity
        for severity in review.SEVERITIES
        if counts.get(severity)
    ]
    if len(parts) == 1:
        return parts[0] + (" finding" if len(findings) == 1 else " findings")
    return str(len(findings)) + " findings: " + ", ".join(parts)


def _print_findings(findings) -> None:
    """Print findings below their summary event, blocking ones included."""
    for finding in findings:
        line = "  [" + finding.severity + "] " + finding.description
        if finding.location:
            line += " (" + finding.location + ")"
        print(line)
        if finding.scenario:
            print("      scenario: " + finding.scenario)
        if finding.impact:
            print("      impact: " + finding.impact)


def _evaluate_run(worktree, exit_code: int, base_branch: str):
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
    if not execution.worktree_has_committed_changes(worktree.path, base_branch):
        return (
            tasks.STATUS_FAILED,
            "the worker made no committed changes relative to " + base_branch
            + " (commit the implementation and run the task again)",
        )
    return tasks.STATUS_IMPLEMENTED, ""


def _dependency_staged_message(task_id: int, dep_id: int, run_id) -> str:
    """The actionable refusal for a dependency staged in another run.

    The message names the run and the exact command that marks the
    dependency DONE; the same command also finalizes the statuses after a
    manual merge, so it is the one instruction the user needs.
    """
    run = "r" + str(run_id) if run_id is not None else "another run"
    return (
        "Cannot execute task t" + str(task_id) + ".\n"
        "\n"
        "Dependency t" + str(dep_id) + " is STAGED in run " + run
        + " and must be\n"
        "marked DONE before this task can execute.\n"
        "\n"
        "Run:\n"
        "    actl run merge " + run + "\n"
        "\n"
        "If you already merged " + run + " manually, execute the\n"
        "same command to finalize its status."
    )


def _staged_block_message(task) -> str:
    """The actionable refusal for a task that is already staged."""
    run = "r" + str(task.run_id) if task.run_id is not None else "another run"
    return (
        "Cannot execute task t" + str(task.id) + ".\n"
        "\n"
        "Task t" + str(task.id) + " is STAGED in run " + run
        + " and must be\n"
        "marked DONE before it can execute.\n"
        "\n"
        "Run:\n"
        "    actl run merge " + run + "\n"
        "\n"
        "If you already merged " + run + " manually, execute the\n"
        "same command to finalize its status."
    )


def _staged_run_message(run) -> str:
    """The actionable refusal for starting a second, unmerged run."""
    name = "r" + str(run.id)
    return (
        "Cannot start a new run: run " + name + " still holds staged\n"
        "tasks and must be integrated first.\n"
        "\n"
        "Run:\n"
        "    actl run merge " + name + "\n"
        "\n"
        "If you already merged " + name + " manually, execute the\n"
        "same command to finalize its status."
    )


def _staged_unmerged_run(actl_dir: Path):
    """Return the oldest non-integrated run that still holds STAGED tasks."""
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        for run in tasks.list_runs(conn):
            if run.status == tasks.RUN_INTEGRATED:
                continue
            staged = any(
                task.status == tasks.STATUS_STAGED
                for task in tasks.run_tasks(conn, run.id)
            )
            if staged:
                return run
        return None
    finally:
        conn.close()


def _new_run_refusal(actl_dir: Path, blocker, requested) -> str:
    """Explain what blocks a new run, task by task first.

    The requested tasks (or every eligible task for ``--all``) are inspected
    before falling back to the generic run refusal, so a task whose own
    dependency is staged in another run gets the exact, actionable message
    that names the dependency and the run to merge.
    """
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        if requested is not None:
            candidates = [tasks.get_task(conn, task_id) for task_id in requested]
        else:
            candidates = [
                task for task in tasks.list_tasks(conn)
                if task.status in _RUNNABLE_STATUSES
            ]
        for task in candidates:
            if task.status == tasks.STATUS_STAGED:
                return _staged_block_message(task)
            for dep, status, dep_run in tasks.unfinished_dependencies(
                conn, task.id, None
            ):
                if status == tasks.STATUS_STAGED:
                    return _dependency_staged_message(task.id, dep, dep_run)
        return _staged_run_message(blocker)
    finally:
        conn.close()


def _require_runnable(task, actl_dir: Path, run) -> None:
    """Refuse tasks that may not be executed.

    A task is eligible only when every dependency is satisfied: DONE (its
    approved work reached the base branch through an integrated run) or
    STAGED in the run ``task`` is currently executing in (the approved work
    is part of the same run branch). An APPROVED dependency is not enough.
    An IMPLEMENTED task proceeds straight to validation and review without
    rerunning the worker; an APPROVED task proceeds straight to staging of
    its approved commit. A NEEDS_CHANGES task from Stage 5 continues from its
    remaining attempts. A NEEDS_HUMAN task refuses to run while its exhausted
    attempt record exists: another automatic cycle would silently restart the
    budget, so the human must intervene (removing ``.actl/attempts/tN``
    deliberately grants a fresh one). A DONE task already finished
    successfully and is never executed again.
    """
    if task.depends_on:
        unfinished = _unfinished_dependencies(actl_dir, task, run)
        if unfinished:
            for dep, status, dep_run in unfinished:
                if status == tasks.STATUS_STAGED:
                    raise tasks.TaskError(
                        _dependency_staged_message(task.id, dep, dep_run)
                    )
            detail = ", ".join(
                "t" + str(dep) + " (" + status + ")"
                for dep, status, _ in unfinished
            )
            raise tasks.TaskError(
                "cannot run task t" + str(task.id) + ": it depends on "
                + detail + "; a task can run only when all its dependencies"
                " are DONE or STAGED in the current run"
            )
    if task.status == tasks.STATUS_DONE:
        raise tasks.TaskError(
            "cannot run task t" + str(task.id) + ": it is already DONE"
        )
    if task.status == tasks.STATUS_STAGED:
        raise tasks.TaskError(_staged_block_message(task))
    if task.status == tasks.STATUS_NEEDS_HUMAN:
        if execution.attempt_state(actl_dir, task.id) is None:
            return
        raise tasks.TaskError(
            "cannot run task t" + str(task.id) + ": it is NEEDS_HUMAN after"
            " its " + str(execution.MAX_WORKER_ATTEMPTS) + " worker attempts;"
            " inspect the task branch, worktree, and logs and intervene by"
            " hand (removing .actl/attempts/t" + str(task.id) + " grants a"
            " deliberate fresh three-attempt budget)"
        )
    if task.status not in (
        tasks.STATUS_PENDING, tasks.STATUS_RUNNING, tasks.STATUS_FAILED,
        tasks.STATUS_IMPLEMENTED, tasks.STATUS_NEEDS_CHANGES,
        tasks.STATUS_APPROVED,
    ):
        raise tasks.TaskError(
            "cannot run task t" + str(task.id)
            + ": unexpected status " + repr(task.status)
        )


def _unfinished_dependencies(actl_dir: Path, task, run):
    """Return the ``(id, status, run_id)`` triples of unsatisfied dependencies."""
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        return tasks.unfinished_dependencies(
            conn, task.id, run.id if run is not None else None
        )
    finally:
        conn.close()


def _running_run(actl_dir: Path):
    """Return the project's RUNNING run, or None."""
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        return tasks.running_run(conn)
    finally:
        conn.close()


def _create_run(actl_dir: Path, root: Path, base_branch: str):
    """Create a new RUNNING run starting at the current base branch commit."""
    execution.require_base(root, base_branch)
    start = execution.ref_commit(root, "refs/heads/" + base_branch)
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        return tasks.create_run(conn, base_branch, start)
    finally:
        conn.close()


def _load_run(actl_dir: Path, run_id: int):
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        return tasks.get_run(conn, run_id)
    finally:
        conn.close()


def _stage_task(actl_dir: Path, task_id: int, run_id: int) -> None:
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        tasks.stage_task(conn, task_id, run_id)
    finally:
        conn.close()


def _set_run_status(actl_dir: Path, run_id: int, status: str) -> None:
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        tasks.set_run_status(conn, run_id, status)
    finally:
        conn.close()


def _list_tasks(actl_dir: Path):
    """Return every task of the project, ordered by id."""
    conn = tasks.connect(actl_dir / tasks.DATABASE_NAME)
    try:
        return tasks.list_tasks(conn)
    finally:
        conn.close()


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
_RUN_ID_RE = re.compile(r"[rR]?([0-9]+)")


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


def _parse_run_id(value: str) -> int:
    match = _RUN_ID_RE.fullmatch(value.strip())
    if match is None:
        raise tasks.TaskError(
            "invalid run id " + repr(value) + " (expected rN or N, e.g. r1)"
        )
    run_id = int(match.group(1))
    if run_id == 0:
        raise tasks.TaskError("invalid run id " + repr(value) + " (ids start at r1)")
    return run_id


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


def _event(message: str) -> None:
    """Print one timestamped orchestration event of ``actl task run``.

    Events are flushed immediately so they appear as they happen even when
    actl's own output is redirected to a pipe or a file.
    """
    print("[" + _utc_now() + "] " + message, flush=True)


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
        help="set the project's worker model (provider/model[#variant]);"
             " required by 'actl task run' whenever the worker runs",
    )
    init.add_argument(
        "--reviewer-model", metavar="MODEL",
        help="set the project's reviewer model (provider/model[#variant]);"
             " required by 'actl task run'",
    )
    init.add_argument(
        "--escalation-worker-model", metavar="MODEL",
        help="set the model for the third and final worker attempt of a"
             " repair cycle (provider/model[#variant]); required by"
             " 'actl task run'",
    )
    init.add_argument(
        "--base", metavar="BRANCH",
        help="base branch for task branches (default: detect, or ask when"
             " ambiguous)",
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
        "run",
        help="execute tasks: worker, validation, review, and staging",
    )
    run.add_argument(
        "ids", nargs="*", metavar="ID",
        help="task id(s) (tN or N); several ids run sequentially in order",
    )
    run.add_argument(
        "--all", action="store_true", dest="all_tasks",
        help="run every eligible task in dependency order",
    )

    run_parser = subparsers.add_parser(
        "run", help="inspect runs and integrate them into the base branch"
    )
    run_sub = run_parser.add_subparsers(dest="run_command", required=True)

    run_sub.add_parser("list", help="list runs")

    run_show = run_sub.add_parser("show", help="show one run")
    run_show.add_argument("id", help="run id (rN or N)")

    run_merge = run_sub.add_parser(
        "merge",
        help="fast-forward a completed run into the configured base branch",
    )
    run_merge.add_argument("id", help="run id (rN or N)")
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
    if args.command == "task":
        handlers = {
            "create": cmd_task_create,
            "list": cmd_task_list,
            "show": cmd_task_show,
            "update": cmd_task_update,
            "delete": cmd_task_delete,
            "run": cmd_task_run,
        }
        return handlers[args.task_command](args)
    handlers = {
        "list": cmd_run_list,
        "show": cmd_run_show,
        "merge": cmd_run_merge,
    }
    return handlers[args.run_command](args)
