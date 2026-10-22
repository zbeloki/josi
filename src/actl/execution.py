"""Task execution: the project lock, task worktrees, and run logs.

``actl task run`` executes one task at a time per project:

* A project-wide ``fcntl.flock`` lock (``.actl/execution.lock``) is taken
  before any work starts. The locked file descriptor is inherited by the
  OpenCode process, so the lock stays held if actl crashes while the worker
  is still running.
* Each task gets one branch (``actl/tN``) and worktree
  (``.actl/worktrees/tN``), created from the latest local ``main`` the first
  time the task runs and reused by later explicit runs.
* One noninteractive OpenCode turn runs inside the worktree and its output is
  streamed into a log file under ``.actl/runs/``.

Worktrees, branches, and logs are never deleted: failed or interrupted work is
preserved for an explicit retry.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LOCK_NAME = "execution.lock"
WORKTREES_DIR = "worktrees"
RUNS_DIR = "runs"
MAIN_BRANCH = "main"


class ExecutionError(Exception):
    """Raised when a task execution cannot proceed."""


@dataclass(frozen=True)
class Worktree:
    """A task's branch and worktree, created or reused."""

    branch: str
    path: Path
    created: bool


def branch_name(task_id: int) -> str:
    """The branch a task's worktree is created on."""
    return "actl/t" + str(task_id)


@contextmanager
def lock(actl_dir: Path):
    """Hold the project-wide execution lock, yielding its file descriptor.

    The lock is exclusive and nonblocking: a second ``actl task run`` fails
    immediately while another execution holds it. The descriptor is kept open
    (and inherited by the OpenCode subprocess) until the context exits.
    """
    path = actl_dir / LOCK_NAME
    try:
        handle = open(path, "a+b")
    except OSError as exc:
        raise ExecutionError(
            "cannot open the execution lock " + str(path) + " (" + str(exc) + ")"
        )
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ExecutionError(
                "another actl task execution is already running in this project"
            )
        yield handle.fileno()
    finally:
        handle.close()


def ensure_worktree(root: Path, actl_dir: Path, task, retry: bool) -> Worktree:
    """Create or reuse the task's branch and worktree.

    The worktree is created from the current local ``main`` on the first
    execution. An explicit retry reuses the registered worktree as-is - never
    resetting, cleaning, or checking out anything - and re-attaches an
    existing branch when the worktree directory was removed.
    """
    branch = branch_name(task.id)
    path = actl_dir / WORKTREES_DIR / ("t" + str(task.id))
    registered = _registered_worktrees(root)
    key = os.path.realpath(str(path))

    if key in registered:
        actual = registered[key]
        if actual != "refs/heads/" + branch:
            raise ExecutionError(
                "worktree " + str(path) + " is not on branch " + branch
                + " (found " + (repr(actual) if actual else "a detached HEAD") + ")"
            )
        if not path.is_dir():
            raise ExecutionError(
                "worktree " + str(path) + " is registered but its directory is"
                " missing; run 'git worktree prune' and run the task again to"
                " re-attach the branch"
            )
        return Worktree(branch=branch, path=path, created=False)
    if path.exists():
        raise ExecutionError(
            str(path) + " exists but is not a registered Git worktree of"
            " this repository"
        )
    if _branch_exists(root, branch):
        if not retry:
            raise ExecutionError(
                "branch " + branch + " already exists; delete it to let actl"
                " create a fresh branch from " + MAIN_BRANCH
            )
        _git(root, "worktree", "add", str(path), branch)
        return Worktree(branch=branch, path=path, created=True)
    _require_main(root)
    _git(root, "worktree", "add", "-b", branch, str(path), MAIN_BRANCH)
    return Worktree(branch=branch, path=path, created=True)


def worktree_branch(path: Path) -> str:
    """Return the branch currently checked out in ``path`` ("HEAD" if detached)."""
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def worktree_dirty(path: Path) -> str:
    """Return ``git status --porcelain`` output for ``path``."""
    return _git(path, "status", "--porcelain").stdout


def open_run_log(actl_dir: Path, task_id: int):
    """Open a new log file for one execution and return ``(path, file)``.

    The name carries a microsecond timestamp and is created exclusively, so a
    run can never truncate an earlier run's log.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = actl_dir / RUNS_DIR / ("t" + str(task_id) + "-" + stamp + ".log")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, path.open("x", encoding="utf-8")
    except OSError as exc:
        raise ExecutionError(
            "cannot open the run log " + str(path) + " (" + str(exc) + ")"
        )


def worker_prompt(task, worktree: Worktree) -> str:
    """The message that tells the worker what to implement."""
    description = task.description.strip() or "(none)"
    return (
        "Implement this task in the current Git worktree"
        " (branch " + worktree.branch + ").\n"
        "\n"
        "Task t" + str(task.id) + ": " + task.title + "\n"
        "\n"
        + description + "\n"
        "\n"
        "Instructions:\n"
        "- Inspect the relevant project files and follow the existing style.\n"
        "- Modify the code as needed to implement the task, without touching\n"
        "  unrelated functionality.\n"
        "- Run the relevant tests or checks to verify your work.\n"
        "- Commit your changes on the current branch with Git. When you finish,\n"
        "  the worktree must be clean: no uncommitted or untracked files\n"
        "  outside .gitignore.\n"
        "- Never merge into " + MAIN_BRANCH + ".\n"
    )


def _require_main(root: Path) -> None:
    if not _ref_exists(root, "refs/heads/" + MAIN_BRANCH):
        raise ExecutionError(
            "branch '" + MAIN_BRANCH + "' does not exist in " + str(root)
            + "; task branches are created from the local " + MAIN_BRANCH + " branch"
        )


def _branch_exists(root: Path, branch: str) -> bool:
    return _ref_exists(root, "refs/heads/" + branch)


def _ref_exists(root: Path, ref: str) -> bool:
    proc = _git(root, "rev-parse", "--verify", "--quiet", ref, check=False)
    return proc.returncode == 0


def _registered_worktrees(root: Path) -> dict:
    """Map real paths to refs from ``git worktree list --porcelain``."""
    output = _git(root, "worktree", "list", "--porcelain").stdout
    worktrees = {}
    for block in output.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if value:
                fields[key] = value
        if "worktree" in fields:
            worktrees[os.path.realpath(fields["worktree"])] = fields.get("branch", "")
    return worktrees


def _git(cwd: Path, *args, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True
        )
    except FileNotFoundError:
        raise ExecutionError("Git is not installed or not on PATH")
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise ExecutionError("git " + " ".join(args) + " failed: " + detail)
    return proc
