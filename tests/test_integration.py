"""End-to-end tests for actl Stage 7: integration and sequential execution.

Every test runs the CLI against a disposable Git repository and the fake
``opencode`` executable. The fake worker and reviewer make each task reach
APPROVED without a real model; the tests then exercise the fast-forward
integration into the configured base branch, the DONE state, dependency-aware
scheduling, and the batch execution modes (``actl task run t1 t2`` and
``actl task run --all``), including every refusal path: a dirty base
worktree, a non-fast-forward merge, a moved task branch, squash branch
options, and an ignored local file the merge would overwrite.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from test_runs import TaskRunTestCase

from actl import tasks

MAJOR = {
    "decision": "changes_requested",
    "findings": [{
        "severity": "major",
        "description": "Blocking problem.",
        "scenario": "the failure happens",
        "impact": "the task is broken",
        "location": "src/feature.py",
    }],
}


@unittest.skipUnless(shutil.which("git"), "git is required")
class IntegrationTestCase(TaskRunTestCase):
    """A project on a ``main`` base branch checked out at the repo root."""

    # -- helpers ----------------------------------------------------------

    def create(self, title, *extra):
        """Create a task and return its id (for example ``"t2"``)."""
        result = self.run_actl("task", "create", "--title", title, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()[-1]

    def dirty_base(self):
        (self.repo / "base-scratch.txt").write_text(
            "dirty\n", encoding="utf-8"
        )

    def clean_base(self):
        path = self.repo / "base-scratch.txt"
        if path.exists():
            path.unlink()

    def approve_without_integrating(self, task_id="t1"):
        """Run a task with a dirty base so it ends APPROVED, not merged."""
        self.dirty_base()
        result = self.run_task(task_id)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Task " + task_id + " \u2192 APPROVED (not merged)", result.stdout)
        self.assertEqual(self.task_status(task_id), "APPROVED")
        return result

    def worker_cwds(self):
        return [
            call["cwd"].rsplit("/", 1)[-1] for call in self.worker_calls()
        ]

    def latest_review(self, task_id=1):
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            return tasks.latest_review(conn, task_id)
        finally:
            conn.close()

    def set_task_status(self, status, task_id=1):
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            tasks.set_task_status(conn, task_id, status)
        finally:
            conn.close()


class MergeTests(IntegrationTestCase):
    def test_approved_task_is_merged_and_marked_done(self):
        self.create("Add the feature")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Review approved", result.stdout)
        self.assertIn("Fast-forward merging...", result.stdout)
        self.assertIn("Task t1 \u2192 DONE", result.stdout)
        self.assertEqual(self.task_status(), "DONE")

        # The base branch fast-forwarded to the reviewed commit; the task
        # branch and worktree are preserved and the repository stays clean.
        self.assertEqual(self.rev("main"), self.rev("actl/t1"))
        stored = self.latest_review()
        self.assertEqual(stored["commit_sha"], self.rev("main"))
        self.assertTrue(self.worktree().is_dir())
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main"
        )

    def test_done_is_recorded_only_after_successful_integration(self):
        self.create("Add the feature")
        self.approve_without_integrating()
        # The merge was refused: the base branch did not move and the task
        # kept its approved state instead of becoming DONE.
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertNotEqual(self.task_status(), "DONE")
        workers = len(self.worker_calls())

        # After the problem is resolved, a plain rerun integrates the exact
        # approved commit without running the worker again.
        self.clean_base()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.rev("main"), self.rev("actl/t1"))
        self.assertEqual(len(self.worker_calls()), workers)

    def test_merge_uses_the_configured_base_branch(self):
        self.git("branch", "develop")
        result = self.run_actl("init", "--base", "develop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.create("Add the feature")

        # ``develop`` is not checked out anywhere: actl fast-forwards it
        # through a temporary merge worktree, and ``main`` never moves.
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.rev("develop"), self.rev("actl/t1"))
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertEqual(
            list((self.repo / ".actl" / "integrate").glob("*")),
            [],
            "the temporary merge worktree must be removed",
        )

    def test_dirty_base_worktree_prevents_merging(self):
        self.create("Add the feature")
        (self.repo / "README.md").write_text("user edit\n", encoding="utf-8")
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integration failed", result.stderr)
        self.assertIn("uncommitted or untracked changes", result.stderr)
        self.assertIn("Task t1 \u2192 APPROVED (not merged)", result.stdout)
        self.assertEqual(self.task_status(), "APPROVED")
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertEqual(
            (self.repo / "README.md").read_text(encoding="utf-8"), "user edit\n",
            "actl must never touch the user's uncommitted changes",
        )

    def test_non_fast_forward_merge_is_refused(self):
        self.create("Add the feature")
        self.approve_without_integrating()
        self.clean_base()
        # The base branch diverges: the approved commit is no longer a
        # descendant of it, so --ff-only must refuse.
        (self.repo / "base-moves.txt").write_text("base\n", encoding="utf-8")
        self.commit("base moves on")
        base_head = self.rev("main")
        self.assertNotEqual(base_head, self.rev("actl/t1"))

        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integration failed", result.stderr)
        self.assertEqual(self.task_status(), "APPROVED")
        self.assertEqual(self.rev("main"), base_head, "main must not move")
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)

    def test_task_branch_moving_after_approval_refuses_merging(self):
        self.create("Add the feature")
        self.approve_without_integrating()
        self.clean_base()
        approved = self.rev("actl/t1")

        # A human commits on the task branch after the approval: the branch
        # no longer points at the approved commit, so nothing may merge.
        (self.worktree() / "human.txt").write_text("human\n", encoding="utf-8")
        self.commit("human change", cwd=self.worktree())
        self.assertNotEqual(self.rev("actl/t1"), approved)

        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("moved after approval", result.stderr)
        self.assertEqual(self.task_status(), "APPROVED")
        self.assertEqual(self.rev("main"), self.base_sha)

    def test_ignored_local_file_is_never_overwritten_by_merging(self):
        # The base worktree ignores a local file that the task's approved
        # commit starts tracking. Ignored files do not make the worktree
        # dirty, so only the merge itself can refuse instead of silently
        # replacing the human's local data.
        (self.repo / ".gitignore").write_text(
            ".actl/\nlocal-notes.txt\n", encoding="utf-8"
        )
        self.commit("ignore the local notes")
        human = "local settings\n"
        (self.repo / "local-notes.txt").write_text(human, encoding="utf-8")
        base = self.rev("main")
        self.create("Track the local notes")
        self.env["ACTL_FAKE_RUN"] = "ignore-collision"

        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("integration failed", result.stderr)
        self.assertIn("local-notes.txt", result.stderr)
        self.assertIn("Task t1 \u2192 APPROVED (not merged)", result.stdout)
        self.assertEqual(self.task_status(), "APPROVED")
        self.assertEqual(self.rev("main"), base, "main must not move")
        self.assertEqual(
            (self.repo / "local-notes.txt").read_text(encoding="utf-8"),
            human,
            "the ignored local file must never be silently overwritten",
        )

        # The human resolves the collision and the same approved commit
        # integrates without rerunning the worker, validation, or review.
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())
        (self.repo / "local-notes.txt").unlink()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.rev("main"), self.rev("actl/t1"))
        self.assertEqual(
            (self.repo / "local-notes.txt").read_text(encoding="utf-8"),
            "tracked default\n",
        )
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)

    def test_branch_squash_options_cannot_fake_a_done_integration(self):
        self.create("Add the feature")
        self.approve_without_integrating()
        self.clean_base()
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())
        # A branch-level option turns a plain merge into a squash that only
        # stages the changes; actl must still fast-forward and may report
        # DONE only once the approved commit is really in the base branch.
        self.git("config", "branch.main.mergeOptions", "--squash")

        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        approved = self.rev("actl/t1")
        self.assertEqual(self.rev("main"), approved)
        self.git("merge-base", "--is-ancestor", approved, "main")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)

    def test_already_approved_task_integrates_without_rerunning_agents(self):
        self.create("Add the feature")
        self.approve_without_integrating()
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())

        self.clean_base()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.rev("main"), self.rev("actl/t1"))
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)

    def test_already_done_task_is_not_executed_again(self):
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        head = self.rev("main")
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())

        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already DONE", result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)
        self.assertEqual(self.rev("main"), head)

    def test_interrupted_integration_never_reruns_the_worker(self):
        # Git integration succeeded but persisting DONE failed: the task is
        # still APPROVED while its approved commit is already in the base
        # branch. The retry recognizes the completed fast-forward, records
        # DONE, and never creates a new implementation.
        self.create("Add the feature")
        self.approve_without_integrating()
        self.clean_base()
        self.git("merge", "--ff-only", "actl/t1")
        self.assertEqual(self.rev("main"), self.rev("actl/t1"))
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())

        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.rev("main"), self.rev("actl/t1"))
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)

    def test_approved_task_without_a_recorded_review_is_refused(self):
        self.create("Add the feature")
        self.set_task_status("APPROVED")
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no approved review is recorded", result.stderr)
        self.assertEqual(self.task_status(), "APPROVED")
        self.assertEqual(self.worker_calls(), [])
        self.assertEqual(self.rev("main"), self.base_sha)

    def test_merge_happens_in_the_worktree_that_checks_out_the_base(self):
        self.git("branch", "develop")
        self.assertEqual(
            self.run_actl("init", "--base", "develop").returncode, 0
        )
        second = self.base / "develop-worktree"
        self.git("worktree", "add", str(second), "develop")
        self.create("Add the feature")

        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.rev("develop"), self.rev("actl/t1"))
        self.assertEqual(
            self.rev("HEAD", cwd=second),
            self.rev("actl/t1"),
            "the worktree with the base branch checked out is updated",
        )
        self.assertEqual(self.rev("main"), self.base_sha)

    def test_merge_worktree_is_removed_after_a_configured_base_merge(self):
        self.git("worktree", "list", "--porcelain")
        self.git("branch", "develop")
        self.assertEqual(
            self.run_actl("init", "--base", "develop").returncode, 0
        )
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        listed = self.git("worktree", "list", "--porcelain").stdout
        self.assertNotIn(".actl/integrate", listed)


class DependencySchedulingTests(IntegrationTestCase):
    def test_approved_dependency_does_not_satisfy_dependents(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        self.approve_without_integrating(t1)

        # APPROVED is not DONE: the dependent stays blocked and untouched.
        result = self.run_task(t2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("depends on t1 (APPROVED)", result.stderr)
        self.assertEqual(self.task_status(t2), "PENDING")
        self.assertFalse(self.worktree(t2).exists())

        # Integrating t1 makes t2 eligible.
        self.clean_base()
        self.assertEqual(self.run_task(t1).returncode, 0)
        self.assertEqual(self.task_status(t1), "DONE")
        self.assertEqual(self.run_task(t2).returncode, 0)
        self.assertEqual(self.task_status(t2), "DONE")

    def test_unfinished_dependencies_refuse_execution(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        result = self.run_task(t2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("depends on t1 (PENDING)", result.stderr)
        self.assertIn("all its dependencies are DONE", result.stderr)
        self.assertEqual(self.worker_calls(), [])
        self.assertEqual(self.task_status(t2), "PENDING")

    def test_explicit_task_list_runs_strictly_in_order(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        t3 = self.create("Third", "--depends-on", t2)
        result = self.run_task(t1, t2, t3)
        self.assertEqual(result.returncode, 0, result.stderr)
        for task_id in (t1, t2, t3):
            self.assertEqual(self.task_status(task_id), "DONE")
        self.assertEqual(self.worker_cwds(), [t1, t2, t3])
        self.assertEqual(self.rev("main"), self.rev("actl/" + t3))
        self.assertIn("Summary: completed: t1, t2, t3", result.stdout)

    def test_explicit_task_list_stops_at_the_first_failure(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(MAJOR)
        t1 = self.create("First")
        t2 = self.create("Second")
        result = self.run_task(t1, t2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(t1), "NEEDS_HUMAN")
        self.assertEqual(self.task_status(t2), "PENDING")
        self.assertNotIn("t2", self.worker_cwds(), "t2 must not run")
        self.assertIn("stopped at t1", result.stdout)
        self.assertIn("Summary:", result.stdout)

    def test_all_runs_in_dependency_order(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        t3 = self.create("Third", "--depends-on", t1)
        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        for task_id in (t1, t2, t3):
            self.assertEqual(self.task_status(task_id), "DONE")
        self.assertEqual(self.worker_cwds(), [t1, t2, t3])
        self.assertEqual(self.rev("main"), self.rev("actl/t3"))
        self.assertIn("Summary: completed: t1, t2, t3", result.stdout)

    def test_all_executes_independent_tasks_in_id_order(self):
        self.create("First")
        self.create("Second")
        self.create("Third")
        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.worker_cwds(), ["t1", "t2", "t3"])

    def test_all_stops_when_a_task_needs_human(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(MAJOR)
        t1 = self.create("First")
        t2 = self.create("Second")
        result = self.run_task("--all")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(t1), "NEEDS_HUMAN")
        self.assertEqual(self.task_status(t2), "PENDING")
        self.assertNotIn("t2", self.worker_cwds(), "t2 must not run")
        self.assertIn("stopped at t1", result.stdout)

    def test_all_never_executes_tasks_with_unfinished_dependencies(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(MAJOR)
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        # t1 already needs a human; t2 can never become ready.
        self.assertNotEqual(self.run_task(t1).returncode, 0)
        self.assertEqual(self.task_status(t1), "NEEDS_HUMAN")
        workers = len(self.worker_calls())

        result = self.run_task("--all")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(t2), "PENDING")
        self.assertEqual(
            len(self.worker_calls()), workers, "t2 must never execute"
        )
        self.assertIn("blocked", result.stdout)
        self.assertIn("progress is blocked", result.stderr)

    def test_all_reports_tasks_that_already_need_a_human(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(MAJOR)
        self.create("First")
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status("t1"), "NEEDS_HUMAN")
        workers = len(self.worker_calls())

        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Summary: completed: none", result.stdout)
        self.assertIn("needs human intervention: t1", result.stdout)
        self.assertEqual(len(self.worker_calls()), workers, "no retry")

    def test_all_with_nothing_to_run_succeeds(self):
        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Summary: completed: none", result.stdout)
        self.assertEqual(self.worker_calls(), [])

    def test_run_requires_ids_or_all(self):
        result = self.run_task()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("at least one task id", result.stderr)

    def test_duplicate_ids_and_all_combined_are_refused(self):
        self.create("First")
        result = self.run_task("t1", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("listed more than once", result.stderr)
        result = self.run_task("--all", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not both", result.stderr)
        self.assertEqual(self.worker_calls(), [])

    def test_explicit_list_refuses_unknown_ids_before_running_anything(self):
        t1 = self.create("First")
        result = self.run_task(t1, "t9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("task t9 does not exist", result.stderr)
        self.assertEqual(self.worker_calls(), [])
        self.assertEqual(self.task_status(t1), "PENDING")


@unittest.skipUnless(shutil.which("git"), "git is required")
class BatchLockTests(IntegrationTestCase):
    def test_batch_refuses_immediately_while_the_lock_is_held(self):
        self.create("First")
        self.create("Second")
        lock_path = self.repo / ".actl" / "execution.lock"
        handle = open(lock_path, "a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = self.run_task("--all")
        finally:
            handle.close()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already running", result.stderr)
        self.assertEqual(self.worker_calls(), [])
        self.assertEqual(self.task_status("t1"), "PENDING")
        self.assertEqual(self.task_status("t2"), "PENDING")

    def test_one_lock_covers_the_whole_batch(self):
        self.create("First")
        self.create("Second")
        release = self.base / "worker.release"
        marker = self.base / "worker.pid"
        self.env["ACTL_FAKE_RUN_MARKER"] = str(marker)
        self.env["ACTL_FAKE_RUN_COUNT_FILE"] = str(self.base / "worker-count")
        self.env["ACTL_FAKE_RUN_SEQUENCE"] = json.dumps(
            [{"wait_for": str(release)}]
        )

        parent = subprocess.Popen(
            [sys.executable, "-m", "actl", "task", "run", "--all"],
            cwd=str(self.repo), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertTrue(self.wait_for_path(marker), "the worker never started")
            # The batch holds the lock while its first task is still running.
            refused = self.run_task("t1")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("already running", refused.stderr)
        finally:
            release.write_text("go", encoding="utf-8")
            try:
                out, err = parent.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                parent.kill()
                out, err = parent.communicate()

        self.assertEqual(parent.returncode, 0, err)
        self.assertEqual(self.task_status("t1"), "DONE")
        self.assertEqual(self.task_status("t2"), "DONE")
        self.assertEqual(self.rev("main"), self.rev("actl/t2"))
        self.assertIn("Summary: completed: t1, t2", out)


class IntegrationOutputTests(IntegrationTestCase):
    def test_terminal_shows_merge_events_and_the_summary(self):
        self.create("First")
        self.create("Second", "--depends-on", "t1")
        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        stdout = result.stdout
        self.assertLess(
            stdout.index("Review approved"), stdout.index("Fast-forward merging...")
        )
        self.assertLess(
            stdout.index("Fast-forward merging..."),
            stdout.index("Task t1 \u2192 DONE"),
        )
        self.assertLess(
            stdout.index("Task t1 \u2192 DONE"), stdout.index("task t2:")
        )
        self.assertIn("Summary: completed: t1, t2", stdout)
        self.assertNotIn("fake opencode:", stdout)
        self.assertNotIn("fake opencode:", result.stderr)

    def test_run_logs_are_preserved_after_integration(self):
        self.create("First")
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertTrue(self.log_paths("t1"))
        log = self.log_text("t1")
        self.assertIn("result: IMPLEMENTED", log)
        self.assertTrue(self.worktree().is_dir())


if __name__ == "__main__":
    unittest.main()
