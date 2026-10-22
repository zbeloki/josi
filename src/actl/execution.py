"""Task execution: the project lock, task worktrees, and run logs.

``actl task run`` executes one task at a time per project:

* A project-wide ``fcntl.flock`` lock (``.actl/execution.lock``) is taken
  before any work starts. The locked file descriptor is inherited by the
  OpenCode process, so the lock stays held if actl crashes while the worker
  is still running.
* Each task gets one branch (``actl/tN``) and worktree
  (``.actl/worktrees/tN``), created from the latest local base branch (the
  project's configured ``base_branch``, passed in by the caller) the first
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
DEFAULT_BASE_CANDIDATES = ("main", "master")


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


def ensure_worktree(
    root: Path, actl_dir: Path, task, retry: bool, base_branch: str
) -> Worktree:
    """Create or reuse the task's branch and worktree.

    The worktree is created from the configured base branch on the first
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
    if branch_exists(root, branch):
        if not retry:
            raise ExecutionError(
                "branch " + branch + " already exists; delete it to let actl"
                " create a fresh branch from " + base_branch
            )
        _git(root, "worktree", "add", str(path), branch)
        return Worktree(branch=branch, path=path, created=True)
    _require_base(root, base_branch)
    _git(
        root, "worktree", "add", "-b", branch, str(path),
        "refs/heads/" + base_branch,
    )
    return Worktree(branch=branch, path=path, created=True)


def repo_has_commits(root: Path) -> bool:
    """Return whether the repository's ``HEAD`` has at least one commit."""
    proc = _git(root, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
    return proc.returncode == 0


def unborn_branch(root: Path):
    """Return the branch ``HEAD`` points at, even when it has no commits.

    Returns None when ``HEAD`` is detached, which an empty repository never
    is in practice.
    """
    proc = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def local_branches(root: Path) -> list:
    """Return every local branch name, sorted."""
    output = _git(
        root, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
    ).stdout
    return [line for line in output.splitlines() if line]


def branch_exists(root: Path, branch: str) -> bool:
    return _ref_exists(root, "refs/heads/" + branch)


def valid_branch_name(root: Path, branch: str) -> bool:
    """Return whether ``branch`` is a valid, literal Git branch name."""
    if not branch or branch.startswith("-"):
        return False
    proc = _git(root, "check-ref-format", "--branch", branch, check=False)
    # check-ref-format expands reflog-style names like "@{-1}"; only a
    # literal name that comes back unchanged is acceptable.
    return proc.returncode == 0 and proc.stdout.strip() == branch


def remote_default_branch(root: Path):
    """Return ``origin/HEAD``'s branch when it also exists as a local branch.

    ``git remote set-head origin -a`` records the remote's default branch as
    ``refs/remotes/origin/HEAD``. The remote default is only usable when the
    matching local branch exists, because task branches are created from a
    local branch. Returns None otherwise.
    """
    prefix = "refs/remotes/origin/"
    proc = _git(
        root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False
    )
    if proc.returncode != 0:
        return None
    ref = proc.stdout.strip()
    if not ref.startswith(prefix):
        return None
    name = ref[len(prefix):]
    return name if branch_exists(root, name) else None


def detect_base_branch(root: Path):
    """Detect the base branch without prompting.

    Priority: the remote default branch (``origin/HEAD``) when the matching
    local branch exists, then the single branch among ``main`` and
    ``master``. Returns None when the choice is ambiguous or no candidate
    exists.
    """
    remote = remote_default_branch(root)
    if remote is not None:
        return remote
    candidates = [
        name for name in DEFAULT_BASE_CANDIDATES if branch_exists(root, name)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def effective_base_branch(root: Path):
    """Return the best base branch for a project without a stored selection.

    Projects initialized before ``base_branch`` existed keep working: the
    branch is detected (remote default, then a single ``main``/``master``),
    a repository without commits falls back to its unborn ``HEAD`` branch,
    and a legacy repository holding both ``main`` and ``master`` keeps the
    historical ``main`` preference. Returns None when no branch can be
    chosen; the caller asks the user to configure one.
    """
    detected = detect_base_branch(root)
    if detected is not None:
        return detected
    if not repo_has_commits(root):
        return unborn_branch(root)
    for name in DEFAULT_BASE_CANDIDATES:
        if branch_exists(root, name):
            return name
    return None


def worktree_branch(path: Path) -> str:
    """Return the branch currently checked out in ``path`` ("HEAD" if detached)."""
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def worktree_dirty(path: Path) -> str:
    """Return ``git status --porcelain`` output for ``path``."""
    return _git(path, "status", "--porcelain").stdout


def worktree_has_committed_changes(path: Path, base_branch: str) -> bool:
    """Return whether HEAD has committed changes relative to the base branch.

    The three-dot diff compares the merge base of the base branch and HEAD
    with HEAD, so commits made on the task branch count even when the base
    branch has advanced. Empty commits and changes that cancel each other out
    do not count.
    """
    ref = "refs/heads/" + base_branch
    proc = _git(path, "diff", "--quiet", ref + "...HEAD", check=False)
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
    raise ExecutionError(
        "git diff " + ref + "...HEAD failed: " + detail
    )


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


def worker_prompt(task, worktree: Worktree, base_branch: str) -> str:
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
        "- Never merge into " + base_branch + ".\n"
    )


def _require_base(root: Path, base_branch: str) -> None:
    """Refuse execution until the base branch exists and has a commit.

    A branch ref only exists once it has at least one commit, so a missing
    ref means either a deleted branch or an unborn branch in a repository
    without commits; the error explains how to create the initial commit.
    """
    if branch_exists(root, base_branch):
        return
    if not repo_has_commits(root):
        if base_branch == unborn_branch(root):
            raise ExecutionError(
                "base branch '" + base_branch + "' has no commits yet; create"
                " an initial commit before running tasks (for example:"
                ' git commit --allow-empty -m "Initial commit")'
            )
        raise ExecutionError(
            "base branch '" + base_branch + "' has no commits yet; create"
            " an initial commit on it before running tasks (for example:"
            " git checkout -b " + base_branch + ' && git commit --allow-empty'
            ' -m "Initial commit")'
        )
    raise ExecutionError(
        "branch '" + base_branch + "' does not exist in " + str(root)
        + "; create it first (for example: git branch " + base_branch
        + ") or run 'actl init --base <branch>' to choose another base branch"
    )


def _ref_exists(root: Path, ref: str) -> bool:
    # show-ref verifies the exact ref name; rev-parse would also accept
    # revision expressions such as "refs/heads/main^".
    proc = _git(root, "show-ref", "--verify", "--quiet", ref, check=False)
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
