"""Command-line interface for actl.

Stage 1 exposes two commands:

* ``actl init`` - register the current Git repository as an actl project.
* ``actl plan`` - start or resume the project's single planner session in the
  OpenCode TUI. ``actl plan --new`` starts a fresh session.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, opencode

PROJECT_DIR = ".actl"
PROJECT_METADATA = "project.json"
PLANNER_STATE = "planner.json"
SCHEMA_VERSION = 1
GITIGNORE_ENTRY = ".actl/"
PLANNER_TITLE = "actl planner"
PLANNER_AGENT_NAME = "actl-planner"
PLANNER_AGENT_PATH = Path(".opencode") / "agents" / (PLANNER_AGENT_NAME + ".md")

# OpenCode loads this file as a custom agent: the frontmatter declares a
# primary agent that can read and explore the project but never edits it.
# Safe Git inspection commands are pre-approved; every other shell command
# requires explicit user approval. The body becomes the agent's system
# prompt. Deployed under .opencode/agents/ so OpenCode discovers it for this
# project.
PLANNER_AGENT_DOC = """\
---
description: "Planning assistant that defines requirements and produces plans without modifying code"
mode: primary
permissions:
  - action: edit
    resource: "*"
    effect: deny
  - action: shell
    resource: "*"
    effect: ask
  - action: shell
    resource: "git status *"
    effect: allow
  - action: shell
    resource: "git log *"
    effect: allow
  - action: shell
    resource: "git diff *"
    effect: allow
  - action: shell
    resource: "git show *"
    effect: allow
  - action: shell
    resource: "git blame *"
    effect: allow
  - action: shell
    resource: "git rev-parse *"
    effect: allow
  - action: shell
    resource: "git ls-files *"
    effect: allow
  - action: shell
    resource: "git grep *"
    effect: allow
---

You are the planning assistant for this repository.

Help the user define software requirements and turn them into a clear,
actionable plan.

- Explore the repository whenever that helps you understand the codebase.
- Inspect and list files with the read, glob, and grep tools instead of shell
  commands such as cat, ls, or grep; those shell commands need confirmation.
- Decompose development goals into concrete, ordered tasks.
- For each task, write a clear description and testable acceptance criteria.
- Do not modify project code.
- Do not execute implementation tasks.

Safe Git inspection commands (git status, git log, git diff, git show,
git blame, git rev-parse, git ls-files, git grep) are pre-approved. Every other
shell command will ask the user for confirmation.

Ask clarifying questions before proposing a plan, and never start implementing.
"""


class ProjectError(Exception):
    """Raised when the project setup is missing or cannot be validated."""


class MetadataError(ProjectError):
    """Raised when actl metadata cannot be read or does not validate."""


def git_toplevel(start: Path) -> Path:
    """Return the root of the Git repository containing ``start``."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise ProjectError("Git is not installed or not on PATH")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ProjectError("not inside a Git repository; run 'git init' first")
    return Path(proc.stdout.strip()).resolve()


def find_project_root(start: Path):
    """Walk upward from ``start`` looking for an initialized actl project."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / PROJECT_DIR).is_dir():
            return candidate
    return None


def load_project_metadata(root: Path) -> dict:
    """Load and validate ``.actl/project.json``."""
    path = root / PROJECT_DIR / PROJECT_METADATA
    if not path.is_file():
        raise MetadataError("missing " + str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataError(str(path) + " is not readable JSON (" + str(exc) + ")")
    session_id = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(session_id, str) or not session_id.startswith("ses"):
        raise MetadataError(str(path) + " does not contain a valid 'session_id'")
    return session_id


def save_planner_session_id(root: Path, session_id: str) -> None:
    path = root / PROJECT_DIR / PLANNER_STATE
    payload = {"session_id": session_id, "created_at": _utc_now()}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def ensure_gitignore(root: Path) -> None:
    """Make sure ``.actl/`` is ignored, without touching existing content."""
    path = root / ".gitignore"
    entry = GITIGNORE_ENTRY
    if path.exists():
        content = path.read_text(encoding="utf-8")
        lines = [line.strip() for line in content.splitlines()]
        if entry in lines or entry.rstrip("/") in lines:
            return
        separator = "" if content == "" or content.endswith("\n") else "\n"
        path.write_text(content + separator + entry + "\n", encoding="utf-8")
    else:
        path.write_text(entry + "\n", encoding="utf-8")


def ensure_planner_agent(root: Path) -> Path:
    """Install the actl planner agent file if it does not exist yet.

    An existing file is left untouched so users can customize their planner.
    """
    path = root / PLANNER_AGENT_PATH
    if path.is_file():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PLANNER_AGENT_DOC, encoding="utf-8")
    except OSError as exc:
        raise ProjectError(
            "could not write the planner agent " + str(path) + " (" + str(exc) + ")"
        )
    return path


def cmd_init(_args) -> int:
    try:
        root = git_toplevel(Path.cwd())
    except ProjectError as exc:
        return _fail(str(exc))
    try:
        opencode.find_binary()
    except opencode.OpenCodeError as exc:
        return _fail(str(exc))

    actl_dir = root / PROJECT_DIR
    metadata_path = actl_dir / PROJECT_METADATA
    if metadata_path.exists():
        try:
            load_project_metadata(root)
        except MetadataError as exc:
            return _fail(
                "existing actl metadata is invalid (" + str(exc) + "); "
                "remove '" + str(actl_dir) + "' and re-run 'actl init'"
            )
        print("actl is already initialized in " + str(root))
    else:
        actl_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "name": root.name,
            "created_at": _utc_now(),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        print("Initialized actl in " + str(root))

    ensure_gitignore(root)
    print("Next step: run 'actl plan' to start a planning session.")
    return 0


def cmd_plan(args) -> int:
    root = find_project_root(Path.cwd())
    if root is None:
        return _fail("no actl project found here; run 'actl init' first")
    try:
        load_project_metadata(root)
    except MetadataError as exc:
        return _fail("invalid actl project metadata: " + str(exc))
    try:
        binary = opencode.find_binary()
    except opencode.OpenCodeError as exc:
        return _fail(str(exc))
    try:
        ensure_planner_agent(root)
    except ProjectError as exc:
        return _fail(str(exc))

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
        if session.agent != PLANNER_AGENT_NAME:
            try:
                opencode.switch_agent(binary, root, session.id, PLANNER_AGENT_NAME)
            except opencode.OpenCodeError as exc:
                return _fail(
                    "cannot select the planner agent for session '"
                    + session.id + "': " + str(exc)
                )
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
    save_planner_session_id(root, session.id)
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

    subparsers.add_parser(
        "init", help="register the current Git repository as an actl project"
    )
    plan = subparsers.add_parser(
        "plan", help="start or resume the project's planner session"
    )
    plan.add_argument(
        "--new", action="store_true", help="start a fresh planner session"
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    return cmd_plan(args)
