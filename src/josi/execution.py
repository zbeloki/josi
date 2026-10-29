"""Task execution: the project lock, run and task worktrees, and logs.

``josi task run`` executes one task at a time inside a run:

* A project-wide ``fcntl.flock`` lock (``.josi/execution.lock``) is taken
  before any work starts. The locked file descriptor is inherited by the
  OpenCode processes, so the lock stays held if josi crashes while a worker
  or reviewer is still running.
* Each run gets one branch (``josi/run/rN``) and worktree
  (``.josi/run-worktrees/rN``), created at the run's recorded start commit
  the first time a task of the run starts and reused while the run is active
  (see ``ensure_run_worktree``). The branch is verified against the run's
  expected commit (see ``require_expected_run_tip``) before any task
  continues, so a commit made by hand in the run worktree is refused instead
  of trusted.
* Each task gets one branch (``josi/tN``) and worktree
  (``.josi/worktrees/tN``), created from the run branch - never directly from
  the base branch - the first time the task runs and reused by later explicit
  runs, so a task starts from the previous staged task's result.
* One noninteractive OpenCode turn runs inside the worktree and its output is
  streamed into a log file under ``.josi/runs/``. Validation and review get
  their own logs in the same directory.
* Before the first worker attempt, a test-generation turn runs in the same
  worktree with the project's test-writer model (see :mod:`josi.testgen`):
  it writes and commits the task's tests, or explicitly decides that the
  task needs none. Its outcome is recorded in ``.josi/testgen/tN`` so a
  retry never repeats a completed stage, and the worker prompt instructs
  the worker to keep the generated tests unchanged.
* The reviewer gets a temporary detached worktree under ``.josi/review/``
  (see ``add_review_worktree``), so its checkout can never touch the task
  worktree.
* An approved task is staged into its run branch and, later, the completed
  run is integrated into the base branch with ``git merge --ff-only`` (see
  ``integrate_commit``): the target branch is fast-forwarded to the exact
  approved commit in the worktree that has it checked out, or - when no
  worktree has it checked out - in a temporary worktree under
  ``.josi/integrate/``. No branch is ever reset, rebased, or forced; the
  merge runs with ``--no-squash`` and ``--no-overwrite-ignore``, so
  inherited branch configuration cannot stage a squash instead, and an
  ignored local file is never silently overwritten.
* Each task records how many worker attempts it has used in
  ``.josi/attempts/tN``, together with the feedback for the next attempt, so
  the three-attempt repair budget survives CLI invocations and an interrupted
  run never silently resets it.

Run worktrees, task worktrees, branches, and logs are never deleted: failed
or interrupted work is preserved for an explicit retry. The temporary
reviewer worktree is the one exception; it is removed after the review.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LOCK_NAME = "execution.lock"
WORKTREES_DIR = "worktrees"
RUN_WORKTREES_DIR = "run-worktrees"
REVIEW_DIR = "review"
INTEGRATE_DIR = "integrate"
BASE_DIR = "base"
RUNS_DIR = "runs"
ATTEMPTS_DIR = "attempts"
TESTGEN_DIR = "testgen"
#: Total worker attempts a task gets before it needs a human (Stage 6).
MAX_WORKER_ATTEMPTS = 3
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
    return "josi/t" + str(task_id)


@contextmanager
def lock(josi_dir: Path):
    """Hold the project-wide execution lock, yielding its file descriptor.

    The lock is exclusive and nonblocking: a second ``josi task run`` fails
    immediately while another execution holds it. The descriptor is kept open
    (and inherited by the OpenCode subprocess) until the context exits.
    """
    path = josi_dir / LOCK_NAME
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
                "another josi task execution is already running in this project"
            )
        yield handle.fileno()
    finally:
        handle.close()


def ensure_worktree(
    root: Path, josi_dir: Path, task, retry: bool, base_branch: str
) -> Worktree:
    """Create or reuse the task's branch and worktree.

    The worktree is created from the configured base branch on the first
    execution. An explicit retry reuses the registered worktree as-is - never
    resetting, cleaning, or checking out anything - and re-attaches an
    existing branch when the worktree directory was removed.

    Creating the branch also records the commit it was created from (see
    :func:`record_branch_base`), so the cumulative review diff keeps the
    original base even if the project's base branch is reconfigured later.

    The base branch is validated before any worktree is created or reused, so
    a retry fails before the worker starts when the base was deleted since
    the previous attempt. Callers pass the run branch here: task branches are
    created from the run branch, never directly from the configured base.
    """
    require_base(root, base_branch)
    branch = branch_name(task.id)
    path = josi_dir / WORKTREES_DIR / ("t" + str(task.id))
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
                "branch " + branch + " already exists; delete it to let josi"
                " create a fresh branch from " + base_branch
            )
        _git(root, "worktree", "add", str(path), branch)
        return Worktree(branch=branch, path=path, created=True)
    _git(
        root, "worktree", "add", "-b", branch, str(path),
        "refs/heads/" + base_branch,
    )
    record_branch_base(josi_dir, task.id, ref_commit(root, "refs/heads/" + branch))
    return Worktree(branch=branch, path=path, created=True)


def require_expected_run_tip(root: Path, run) -> None:
    """Refuse to continue when the run branch moved unexpectedly.

    A run branch may only ever point at the commit the run itself recorded:
    the start commit, advanced by successful staging of reviewer-approved
    tasks. Any other commit - for example one a human made in the run
    worktree - is refused, and the branch is never reset or rewritten. Runs
    created before the expected commit existed (no recorded value) are not
    checked.
    """
    expected = getattr(run, "expected_commit", None)
    if expected is None:
        return
    actual = ref_commit(root, "refs/heads/" + run.branch)
    if actual != expected:
        raise ExecutionError(
            "run branch " + run.branch + " moved unexpectedly: expected "
            + expected[:12] + ", found " + actual[:12]
            + "; refusing to continue without explicit intervention"
        )


def ensure_run_worktree(root: Path, josi_dir: Path, run) -> Worktree:
    """Create or reuse the run's branch and worktree.

    The run branch is created at the run's recorded start commit the first
    time a task of the run starts, and its worktree lives under
    ``.josi/run-worktrees/rN``. An interrupted run reuses both as they are -
    never resetting, cleaning, or checking out anything - after verifying
    that the branch still points at the commit the run recorded. A run whose
    worktree directory was removed re-attaches the existing branch, and a
    branch that already staged tasks is never silently recreated from the
    start commit. The base branch is validated first, so a run fails before
    any worktree work when the base disappeared while the run was waiting.
    """
    require_base(root, run.base_branch)
    branch = run.branch
    path = josi_dir / RUN_WORKTREES_DIR / ("r" + str(run.id))
    registered = _registered_worktrees(root)
    key = os.path.realpath(str(path))

    if key in registered:
        if not branch_exists(root, branch):
            raise ExecutionError(
                "run branch " + branch + " no longer exists; the run cannot"
                " continue without it (restore the branch or intervene by hand)"
            )
        require_expected_run_tip(root, run)
        actual = registered[key]
        if actual != "refs/heads/" + branch:
            raise ExecutionError(
                "run worktree " + str(path) + " is not on branch " + branch
                + " (found " + (repr(actual) if actual else "a detached HEAD") + ")"
            )
        if not path.is_dir():
            raise ExecutionError(
                "run worktree " + str(path) + " is registered but its directory"
                " is missing; run 'git worktree prune' and run the task again to"
                " re-attach the run branch"
            )
        return Worktree(branch=branch, path=path, created=False)
    if path.exists():
        raise ExecutionError(
            str(path) + " exists but is not a registered Git worktree of"
            " this repository"
        )
    if branch_exists(root, branch):
        require_expected_run_tip(root, run)
        _git(root, "worktree", "add", str(path), branch)
        return Worktree(branch=branch, path=path, created=True)
    expected = getattr(run, "expected_commit", None)
    if expected is not None and expected != run.start_commit:
        raise ExecutionError(
            "run branch " + branch + " is missing although the run already"
            " staged tasks; refusing to recreate it from the start commit"
            " (restore the branch or intervene by hand)"
        )
    _git(
        root, "worktree", "add", "-b", branch, str(path), run.start_commit,
    )
    return Worktree(branch=branch, path=path, created=True)


def repo_has_commits(root: Path) -> bool:
    """Return whether the repository contains at least one commit.

    ``HEAD`` alone is not enough: after ``git checkout --orphan`` it is
    unborn even though other branches still hold commits. The repository is
    only treated as empty when nothing reachable has ever been committed.
    """
    proc = _git(root, "rev-list", "--all", "--max-count=1", check=False)
    return bool(proc.stdout.strip())


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
    """Return every local branch name, sorted.

    Full ref names are used and the ``refs/heads/`` prefix is stripped in
    Python: the short form would pick an unambiguous abbreviation such as
    ``heads/main`` when a tag shadows the branch name.
    """
    output = _git(
        root, "for-each-ref", "--format=%(refname)", "refs/heads/"
    ).stdout
    prefix = "refs/heads/"
    return [
        line[len(prefix):]
        for line in output.splitlines()
        if line.startswith(prefix)
    ]


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


def head_commit(cwd: Path) -> str:
    """Return the commit ``HEAD`` points at in ``cwd``."""
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def ref_commit(root: Path, ref: str) -> str:
    """Return the commit a full ref name points at, or raise."""
    return _git(root, "rev-parse", "--verify", ref).stdout.strip()


def merge_base(root: Path, ref_a: str, ref_b: str) -> str:
    """Return the best common ancestor of two commits/refs."""
    return _git(root, "merge-base", ref_a, ref_b).stdout.strip()


def is_ancestor(root: Path, commit: str, ref: str) -> bool:
    """Return whether ``commit`` is an ancestor of (or equal to) ``ref``."""
    proc = _git(root, "merge-base", "--is-ancestor", commit, ref, check=False)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
    raise ExecutionError(
        "git merge-base --is-ancestor " + commit + " " + ref + " failed: "
        + detail
    )


def worktree_tracked_dirty(path: Path) -> str:
    """Return ``git status --porcelain`` output for tracked files only.

    Untracked files are ignored: they are part of no commit and cannot change
    what a reviewer sees, while validation tools routinely create caches and
    reports.
    """
    return _git(path, "status", "--porcelain", "--untracked-files=no").stdout


def commit_diff(cwd: Path, base_commit: str, commit: str) -> str:
    """Return the diff from ``base_commit`` to ``commit``."""
    return _git(cwd, "diff", base_commit, commit).stdout


def add_review_worktree(root: Path, path: Path, commit: str) -> None:
    """Create a detached temporary worktree at ``commit`` for the reviewer."""
    _git(root, "worktree", "add", "--detach", str(path), commit)


def record_branch_base(josi_dir: Path, task_id: int, commit: str) -> None:
    """Record the commit a task branch was created from."""
    path = josi_dir / BASE_DIR / ("t" + str(task_id))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(commit + "\n", encoding="utf-8")
    except OSError as exc:
        raise ExecutionError(
            "cannot record the branch base " + str(path) + " (" + str(exc) + ")"
        )


def branch_base(josi_dir: Path, task_id: int):
    """Return the recorded branch-creation commit, or None.

    Branches created before josi recorded this (Stage 4 projects) have no
    record; the caller falls back to the merge base with the configured base
    branch.
    """
    path = josi_dir / BASE_DIR / ("t" + str(task_id))
    try:
        commit = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return commit or None


def record_testgen_outcome(josi_dir: Path, task_id: int, decision: str) -> None:
    """Record that the test-generation stage completed with ``decision``.

    The record is written only after the stage succeeded, so an interrupted
    or failed stage never suppresses a later one.
    """
    path = josi_dir / TESTGEN_DIR / ("t" + str(task_id))
    payload = {"decision": decision}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ExecutionError(
            "cannot record the test-generation outcome " + str(path)
            + " (" + str(exc) + ")"
        )


def testgen_outcome(josi_dir: Path, task_id: int):
    """Return the recorded test-generation decision, or None.

    A missing record means the stage has not completed yet. A record that
    cannot be read or parsed is ignored rather than fatal: the stage would
    simply run again, which a broken marker must never silently prevent.
    """
    path = josi_dir / TESTGEN_DIR / ("t" + str(task_id))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    decision = data.get("decision")
    return decision if isinstance(decision, str) else None


def clear_testgen_outcome(josi_dir: Path, task_id: int) -> None:
    """Delete the task's recorded test-generation outcome.

    Used only by the explicit ``josi task reset`` intervention: the record
    belongs to the branch and worktree the reset discards, so keeping it
    would make the next execution skip a stage that never ran on the fresh
    branch. A missing record is not an error.
    """
    path = josi_dir / TESTGEN_DIR / ("t" + str(task_id))
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ExecutionError(
            "cannot remove the test-generation outcome " + str(path)
            + " (" + str(exc) + ")"
        )


def remove_temporary_worktree(root: Path, path: Path) -> str:
    """Remove a temporary worktree; return an error text or "".

    Removal is forced: a reviewer or merge may have left untracked or
    modified files in its disposable checkout, and those changes are never
    rolled back into a task worktree or the base worktree. A failure is
    reported but never blocks a verdict or a completed merge.
    """
    proc = _git(root, "worktree", "remove", "--force", str(path), check=False)
    if proc.returncode == 0:
        return ""
    return proc.stderr.strip() or proc.stdout.strip() or "git worktree remove failed"


def worktree_for_branch(root: Path, branch: str):
    """Return the path of the worktree that has ``branch`` checked out.

    Git allows a branch to be checked out in at most one worktree, so the
    match is unique. Returns None when no worktree has the branch checked out
    (a bare or detached checkout) - the caller decides where to merge.
    """
    wanted = "refs/heads/" + branch
    for path, ref in _registered_worktrees(root).items():
        if ref == wanted:
            return Path(path)
    return None


def integrate_commit(
    root: Path, josi_dir: Path, base_branch: str, branch: str, commit: str
) -> Path:
    """Fast-forward ``base_branch`` to ``commit``; return the merge worktree.

    Used for both steps of the run model: staging an approved task into its
    run branch (``base_branch`` is the run branch) and integrating a
    completed run into the configured base branch (``base_branch`` is the
    base branch, ``commit`` the recorded final commit).

    The commit is merged exactly as it was validated and approved: the
    source branch must still point at it, the target branch must exist, and
    the worktree holding the target branch (or a temporary one, when no
    worktree has it checked out) must be clean before ``git merge --ff-only``
    runs. A non-fast-forward merge, a moved source branch, a missing target
    branch, and a dirty target worktree all raise :class:`ExecutionError`
    without changing anything: josi never resets, rebases, force-pushes, or
    resolves conflicts, so the caller can keep the task APPROVED (or the run
    COMPLETE) for a deliberate retry.

    The merge always runs with ``--no-squash`` and ``--no-overwrite-ignore``,
    so branch configuration cannot turn it into a squash that only stages
    the changes, and an ignored local file in the target worktree (which
    does not make it dirty) is refused instead of silently overwritten.
    After the merge, the target branch must really contain the commit;
    otherwise the integration fails and the status stays unchanged.

    A temporary merge worktree is removed on every path; the branch ref keeps
    the merge either way.
    """
    current = ref_commit(root, "refs/heads/" + branch)
    if current != commit:
        raise ExecutionError(
            "branch " + branch + " moved after approval: expected "
            + commit[:12] + ", found " + current[:12]
            + "; refusing to merge"
        )
    if not branch_exists(root, base_branch):
        raise ExecutionError(
            "branch '" + base_branch + "' does not exist in " + str(root)
            + "; refusing to merge"
        )
    worktree = worktree_for_branch(root, base_branch)
    temporary = None
    if worktree is None:
        temporary = (
            josi_dir / INTEGRATE_DIR
            / (branch.replace("/", "-") + "-" + timestamp())
        )
        try:
            # The short branch name (not the full ref) makes git check the
            # branch out instead of detaching HEAD. Verifying the checkout
            # below still refuses safely when another ref shadows the name.
            _git(root, "worktree", "add", str(temporary), base_branch)
        except ExecutionError:
            if temporary.exists():
                remove_temporary_worktree(root, temporary)
            raise
        worktree = temporary
    try:
        checked_out = worktree_branch(worktree)
        if checked_out != base_branch:
            raise ExecutionError(
                "worktree " + str(worktree) + " has '" + checked_out
                + "' checked out instead of '" + base_branch
                + "'; refusing to merge"
            )
        dirty = worktree_dirty(worktree)
        if dirty:
            raise ExecutionError(
                "the worktree " + str(worktree)
                + " has uncommitted or untracked changes; commit or remove"
                " them before merging"
            )
        _git(
            worktree, "merge", "--ff-only", "--no-squash",
            "--no-overwrite-ignore", commit,
        )
        # Command-line options override branch configuration, but never
        # report a completed integration unless the target branch really
        # contains the commit (a squash, for example, stages the changes and
        # exits successfully without moving the branch).
        if not is_ancestor(root, commit, "refs/heads/" + base_branch):
            raise ExecutionError(
                "the merge reported success but commit "
                + commit[:12] + " is not an ancestor of " + base_branch
                + "; refusing to report the integration as complete"
            )
    except BaseException:
        if temporary is not None:
            remove_temporary_worktree(root, temporary)
        raise
    if temporary is not None:
        problem = remove_temporary_worktree(root, temporary)
        if problem:
            raise ExecutionError(
                "the merge finished but the temporary worktree "
                + str(temporary) + " could not be removed (" + problem + ")"
            )
    return worktree


def attempt_state(josi_dir: Path, task_id: int):
    """Return the task's recorded ``(attempts, feedback)``, or None.

    ``attempts`` counts the worker attempts the task has used in its current
    repair cycle; ``feedback`` is the text the next worker attempt must fix
    (a failed validation or reviewer rejection), or "".

    A missing record means no attempt was recorded yet. A corrupt record is
    an error instead of an implicit reset: restarting josi must never hand a
    task a fresh attempt budget by accident.
    """
    path = josi_dir / ATTEMPTS_DIR / ("t" + str(task_id))
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ExecutionError(
            "cannot read the attempt record " + str(path) + " (" + str(exc) + ")"
        )
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ExecutionError(
            "the attempt record " + str(path) + " is not readable JSON ("
            + str(exc) + "); fix or remove it before running the task again"
            " (removing it grants a fresh attempt budget deliberately)"
        )
    attempts = data.get("attempts") if isinstance(data, dict) else None
    feedback = data.get("feedback") if isinstance(data, dict) else None
    if feedback is None:
        feedback = ""
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ExecutionError(
            "the attempt record " + str(path) + " has an invalid 'attempts'"
            " value; fix or remove it before running the task again"
        )
    if not isinstance(feedback, str):
        raise ExecutionError(
            "the attempt record " + str(path) + " has an invalid 'feedback'"
            " value; fix or remove it before running the task again"
        )
    return attempts, feedback


def record_attempt_state(
    josi_dir: Path, task_id: int, attempts: int, feedback: str,
) -> None:
    """Persist the task's ``attempts`` count and next-attempt ``feedback``."""
    path = josi_dir / ATTEMPTS_DIR / ("t" + str(task_id))
    payload = {"attempts": attempts, "feedback": feedback or ""}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ExecutionError(
            "cannot write the attempt record " + str(path) + " (" + str(exc) + ")"
        )


def clear_attempt_state(josi_dir: Path, task_id: int) -> None:
    """Delete the task's attempt record, granting a fresh attempt budget.

    Used only by the explicit ``josi task reset`` intervention. A missing
    record is not an error: the task never spent an attempt.
    """
    path = josi_dir / ATTEMPTS_DIR / ("t" + str(task_id))
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ExecutionError(
            "cannot remove the attempt record " + str(path) + " (" + str(exc) + ")"
        )


def reset_task_worktree(root: Path, josi_dir: Path, task_id: int) -> None:
    """Discard a task's branch, worktree, and recorded base for a reset.

    ``josi task reset`` is the explicit human intervention that starts a task
    over: the registered worktree is force-removed (any uncommitted work goes
    with it), the task branch is deleted, and the recorded branch base is
    dropped, so the next execution creates a fresh branch from the run branch.
    Only the task's own worktree and branch are touched - the run branch and
    every other task are left alone. A task that never ran has neither, and
    the call is a no-op.
    """
    branch = branch_name(task_id)
    path = josi_dir / WORKTREES_DIR / ("t" + str(task_id))
    registered = _registered_worktrees(root)
    key = os.path.realpath(str(path))
    if key in registered:
        problem = remove_temporary_worktree(root, path)
        if problem:
            raise ExecutionError(
                "cannot remove the task worktree " + str(path) + " (" + problem + ")"
            )
    elif path.exists():
        raise ExecutionError(
            str(path) + " exists but is not a registered Git worktree of this"
            " repository; remove it by hand before resetting the task"
        )
    if branch_exists(root, branch):
        _git(root, "branch", "-D", branch)
    base = josi_dir / BASE_DIR / ("t" + str(task_id))
    try:
        base.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ExecutionError(
            "cannot remove the recorded branch base " + str(base)
            + " (" + str(exc) + ")"
        )


def timestamp() -> str:
    """Return a UTC timestamp usable in file and worktree names."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def open_stage_log(josi_dir: Path, task_id: int, stage):
    """Open a new log file for one execution stage and return ``(path, file)``.

    The name carries a microsecond timestamp and is created exclusively, so a
    run can never truncate an earlier run's log. ``stage`` (for example
    ``"tests"``, ``"validation"``, or ``"review"``) is appended to the
    worker's plain ``tN-<timestamp>.log`` name.
    """
    name = "t" + str(task_id) + "-" + timestamp()
    if stage:
        name += "-" + stage
    path = josi_dir / RUNS_DIR / (name + ".log")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, path.open("x", encoding="utf-8")
    except OSError as exc:
        raise ExecutionError(
            "cannot open the " + (stage or "run") + " log " + str(path)
            + " (" + str(exc) + ")"
        )


def open_run_log(josi_dir: Path, task_id: int):
    """Open a new log file for one worker execution and return ``(path, file)``."""
    return open_stage_log(josi_dir, task_id, None)


def worker_prompt(
    task, worktree: Worktree, base_branch: str, attempt=None, total=None, feedback="",
) -> str:
    """The message that tells the worker what to implement.

    On a repair attempt (``attempt`` greater than 1) the worker is told to
    continue from the existing worktree and commits and receives the
    ``feedback`` that explains why the previous attempt was rejected: the
    failed validation command and its output, or the blocking reviewer
    findings. The branch is never reset or discarded.

    The instructions always tell the worker to keep the tests the
    test-generation stage produced: the worker implements the task around
    the generated tests and never modifies, weakens, or deletes them.
    """
    description = task.description.strip() or "(none)"
    lines = [
        "Implement this task in the current Git worktree"
        " (branch " + worktree.branch + ").",
        "",
        "Task t" + str(task.id) + ": " + task.title,
        "",
        description,
        "",
    ]
    if attempt is not None and attempt > 1:
        lines += [
            "This is worker attempt " + str(attempt) + " of " + str(total)
            + " after a previous attempt was not accepted. The previous"
            " implementation is already committed on the current branch:"
            " continue from the existing worktree and commits, keep the"
            " earlier commits, and fix the existing implementation. Do not"
            " reset the branch or discard previous commits.",
            "",
            feedback.strip()
            or "No validation or review feedback is available for this"
            " attempt; inspect the previous implementation and fix whatever"
            " still prevents it from being accepted.",
            "",
        ]
    lines += [
        "Instructions:",
        "- Inspect the relevant project files and follow the existing style.",
        "- Modify the code as needed to implement the task, without touching",
        "  unrelated functionality.",
        "- Do not write, add, modify, weaken, or delete tests. Tests for this",
        "  task are produced by a separate test-generation stage: keep any",
        "  existing tests as they are and implement the task so they pass.",
        "- Run the relevant tests or checks to verify your work.",
        "- Commit your changes on the current branch with Git. When you finish,",
        "  the worktree must be clean: no uncommitted or untracked files",
        "  outside .gitignore.",
        "- Never merge into " + base_branch + ".",
    ]
    return "\n".join(lines)


def require_base(root: Path, base_branch: str) -> None:
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
        + ") or run 'josi init --base <branch>' to choose another base branch"
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
