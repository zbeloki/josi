"""End-to-end tests for josi Stage 7: run-level integration.

Every test runs the CLI against a disposable Git repository and the fake
``opencode`` executable. Execution is grouped into runs: ``josi task run``
creates the run branch ``josi/run/rN`` from the configured base branch, stages
every approved task into it, and never touches the base branch. ``josi run
merge rN`` integrates a completed run into the base branch exactly once and
turns its STAGED tasks DONE; the tests cover fast-forward integration, a
manual merge (plain or merge commit), a non-fast-forward that needs human
intervention, idempotent repeats, dependency handling inside and across runs,
failed runs preserving staged work, and the execution lock over a batch.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_runs import TaskRunTestCase

from josi import tasks

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
        result = self.run_josi("task", "create", "--title", title, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()[-1]

    def dirty_base(self):
        (self.repo / "base-scratch.txt").write_text("dirty\n", encoding="utf-8")

    def clean_base(self):
        path = self.repo / "base-scratch.txt"
        if path.exists():
            path.unlink()

    def worker_cwds(self):
        return [
            call["cwd"].rsplit("/", 1)[-1] for call in self.worker_calls()
        ]

    def latest_review(self, task_id=1):
        conn = tasks.connect(self.repo / ".josi" / "tasks.db")
        try:
            return tasks.latest_review(conn, task_id)
        finally:
            conn.close()

    def set_task_status(self, status, task_id=1):
        conn = tasks.connect(self.repo / ".josi" / "tasks.db")
        try:
            tasks.set_task_status(conn, task_id, status)
        finally:
            conn.close()

    def run_show(self, run_id="r1"):
        result = self.run_josi("run", "show", run_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = {"output": result.stdout}
        for line in result.stdout.splitlines():
            key, _, value = line.partition(": ")
            if key != line:
                fields[key] = value
        return fields


class RunBranchTests(IntegrationTestCase):
    def test_run_branch_and_worktree_are_created_from_the_base(self):
        self.create("Add the feature")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Created run r1", result.stdout)

        worktree = self.repo / ".josi" / "run-worktrees" / "r1"
        self.assertTrue(worktree.is_dir())
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree).stdout.strip(),
            "josi/run/r1",
        )
        listed = self.git("worktree", "list", "--porcelain").stdout
        self.assertIn(str(worktree), listed)

        # The run started exactly at the base commit and finished at the
        # approved task commit; the base branch never moved.
        shown = self.run_show()
        self.assertEqual(shown["status"], "COMPLETE")
        self.assertEqual(shown["branch"], "josi/run/r1")
        self.assertEqual(shown["base_branch"], "main")
        self.assertEqual(shown["start_commit"], self.base_sha)
        self.assertEqual(shown["final_commit"], self.rev("josi/t1"))
        self.assertEqual(self.rev("josi/run/r1"), self.rev("josi/t1"))
        self.assertEqual(self.rev("main"), self.base_sha)

    def test_run_list_shows_status_branch_and_tasks(self):
        self.create("First")
        self.assertEqual(self.run_task("t1").returncode, 0)
        result = self.run_josi("run", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "r1  COMPLETE  josi/run/r1  t1"
        )

        empty = self.run_josi("run", "list")
        self.assertEqual(empty.returncode, 0)

    def test_run_show_refuses_an_unknown_run(self):
        result = self.run_josi("run", "show", "r9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run r9 does not exist", result.stderr)
        result = self.run_josi("run", "merge", "r9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run r9 does not exist", result.stderr)

    def test_multiple_tasks_share_one_run_and_stage_in_order(self):
        t1 = self.create("First")
        t2 = self.create("Second")
        t3 = self.create("Third")
        result = self.run_task(t1, t2, t3)
        self.assertEqual(result.returncode, 0, result.stderr)
        for task_id in (t1, t2, t3):
            self.assertEqual(self.task_status(task_id), "STAGED")
        self.assertEqual(self.worker_cwds(), [t1, t2, t3])

        # One run holds all three tasks; the run branch advanced with every
        # staged task and the base branch stayed put.
        self.assertEqual(self.rev("josi/run/r1"), self.rev("josi/t3"))
        self.assertEqual(self.rev("josi/t2~1"), self.rev("josi/t1"))
        self.assertEqual(self.rev("josi/t3~1"), self.rev("josi/t2"))
        self.assertEqual(self.rev("main"), self.base_sha)
        shown = self.run_show()
        self.assertIn("tasks:\nt1  STAGED  First", shown["output"])

    def test_task_starts_from_the_previous_staged_task_result(self):
        self.create("First")
        self.create("Second")
        self.assertEqual(self.run_task("t1", "t2").returncode, 0)

        # The second task started from the run branch after the first task
        # was staged: it sees the first task's committed file.
        content = (self.worktree("t2") / "worker-output.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("worker change", content)
        self.assertEqual(self.rev("josi/t2~1"), self.rev("josi/t1"))

    def test_base_branch_remains_unchanged_during_execution(self):
        self.create("First")
        self.create("Second", "--depends-on", "t1")
        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main"
        )
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_failed_batch_preserves_staged_work_and_stays_resumable(self):
        self.env["JOSI_FAKE_RUN_SEQUENCE"] = json.dumps(
            [{"behavior": "commit"}, {"behavior": "commit", "exit": 1}]
        )
        self.env["JOSI_FAKE_RUN_COUNT_FILE"] = str(self.base / "worker-count")
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        result = self.run_task(t1, t2)
        self.assertNotEqual(result.returncode, 0)

        # The batch stopped at t2 but t1's approved work is preserved in the
        # run branch; the base branch is untouched and the run stays open and
        # resumable - a partial plan is never integrated.
        self.assertEqual(self.task_status(t1), "STAGED")
        self.assertEqual(self.task_status(t2), "FAILED")
        self.assertEqual(self.rev("josi/run/r1"), self.rev("josi/t1"))
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertEqual(self.run_status("r1"), "RUNNING")
        self.assertTrue(self.worktree("t2").is_dir(), "worktree preserved")
        self.assertTrue(self.log_paths("t2"), "logs preserved")
        refused = self.integrate()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("still marked RUNNING", refused.stderr)
        self.assertEqual(self.rev("main"), self.base_sha)

        # Fixing the failure resumes the same run: the dependency is
        # satisfied within it and both tasks end staged before integration.
        result = self.run_task(t2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(t1), "STAGED")
        self.assertEqual(self.task_status(t2), "STAGED")
        self.assertEqual(self.run_status("r1"), "COMPLETE")
        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.task_status(t1), "DONE")
        self.assertEqual(self.task_status(t2), "DONE")

    def test_unmerged_run_with_staged_tasks_blocks_a_new_run(self):
        # Only one unfinished, unmerged run may exist: a completed run that
        # still holds staged tasks must be integrated before new work starts,
        # even for an independent task.
        t1 = self.create("First")
        t2 = self.create("Second")
        self.assertEqual(self.run_task(t1).returncode, 0)
        workers = len(self.worker_calls())

        result = self.run_task(t2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr,
            "josi: error: "
            "Cannot start a new run: run r1 still holds staged\n"
            "tasks and must be integrated first.\n"
            "\n"
            "Run:\n"
            "    josi run merge r1\n"
            "\n"
            "If you already merged r1 manually, execute the\n"
            "same command to finalize its status.\n",
        )
        self.assertNotIn("t2", self.worker_cwds())
        self.assertEqual(self.task_status(t2), "PENDING")

        self.assertEqual(self.integrate().returncode, 0)
        result = self.run_task(t2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(t2), "STAGED")
        self.assertEqual(self.run_status("r2"), "COMPLETE")
        self.assertEqual(len(self.worker_calls()), workers + 1)

    def test_run_stopped_before_staging_is_resumed_by_a_retry(self):
        # A run stopped before staging anything also stays open: the retry
        # resumes the same run instead of starting a second one.
        self.create("First")
        self.env["JOSI_FAKE_RUN_EXIT"] = "1"
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(self.run_status("r1"), "RUNNING")

        self.env["JOSI_FAKE_RUN_EXIT"] = "0"
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.run_status("r1"), "COMPLETE")
        self.assertEqual(self.rev("main"), self.base_sha)
        listing = self.run_josi("run", "list")
        self.assertEqual(len(listing.stdout.strip().splitlines()), 1)

    def test_run_branch_movement_refuses_resumption(self):
        # The run records the only commit its branch may point at: a commit
        # made by hand in the run worktree is refused instead of trusted.
        self.create("First")
        self.create("Second", "--depends-on", "t1")
        self.assertEqual(self.run_task("t1").returncode, 0)
        conn = tasks.connect(self.repo / ".josi" / "tasks.db")
        try:
            tasks.set_run_status(conn, 1, tasks.RUN_RUNNING)
        finally:
            conn.close()
        run_worktree = self.run_worktree()
        (run_worktree / "human.txt").write_text("human\n", encoding="utf-8")
        self.commit("human change on the run branch", cwd=run_worktree)
        human = self.rev("josi/run/r1")

        result = self.run_task("t2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("moved unexpectedly", result.stderr)
        self.assertEqual(self.task_status("t2"), "PENDING")
        self.assertNotIn("t2", self.worker_cwds())
        self.assertEqual(self.rev("josi/run/r1"), human, "never reset")
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertEqual(self.run_status("r1"), "RUNNING")
        refused = self.integrate()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("still marked RUNNING", refused.stderr)
        self.assertFalse((self.repo / "human.txt").exists())

    def test_unreviewed_run_branch_commit_is_never_integrated(self):
        # Regression: a commit made by hand on the run branch while a task
        # executes must never become the recorded final commit or reach the
        # base branch.
        release = self.base / "worker.release"
        self.env["JOSI_FAKE_RUN_SEQUENCE"] = json.dumps(
            [{"behavior": "commit"},
             {"behavior": "commit", "wait_for": str(release)}]
        )
        count = self.base / "worker-count"
        self.env["JOSI_FAKE_RUN_COUNT_FILE"] = str(count)
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)

        parent = subprocess.Popen(
            [sys.executable, "-m", "josi", "task", "run", t1, t2],
            cwd=str(self.repo), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if (
                    count.exists()
                    and count.read_text(encoding="utf-8").strip() == "2"
                ):
                    break
                time.sleep(0.05)
            else:
                self.fail("the second worker never started")
            self.assertEqual(self.task_status(t1), "STAGED")
            (self.run_worktree() / "not-reviewed.txt").write_text(
                "human experiment\n", encoding="utf-8"
            )
            self.commit(
                "unreviewed change on the run branch",
                cwd=self.run_worktree(),
            )
            human = self.rev("josi/run/r1")
        finally:
            release.write_text("go", encoding="utf-8")
            try:
                out, err = parent.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                parent.kill()
                out, err = parent.communicate()

        self.assertNotEqual(parent.returncode, 0)
        self.assertIn("staging failed", err)
        self.assertEqual(self.task_status(t1), "STAGED")
        self.assertEqual(self.task_status(t2), "APPROVED")
        self.assertEqual(self.rev("josi/run/r1"), human)
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertEqual(self.run_status("r1"), "RUNNING")
        self.assertNotEqual(self.latest_review(2)["commit_sha"], human)

        refused = self.integrate()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("still marked RUNNING", refused.stderr)
        self.assertFalse((self.repo / "not-reviewed.txt").exists())


class RunMergeTests(IntegrationTestCase):
    def test_run_merge_fast_forwards_the_base_and_marks_tasks_done(self):
        t1 = self.create("First")
        t2 = self.create("Second")
        self.assertEqual(self.run_task(t1, t2).returncode, 0)
        self.assertEqual(self.rev("main"), self.base_sha)

        result = self.integrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("integrated into main", result.stdout)
        self.assertEqual(self.rev("main"), self.rev("josi/run/r1"))
        self.assertEqual(self.task_status(t1), "DONE")
        self.assertEqual(self.task_status(t2), "DONE")
        self.assertEqual(self.run_status(), "INTEGRATED")
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main"
        )
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_repeated_run_merge_is_safe(self):
        self.create("First")
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.integrate().returncode, 0)
        head = self.rev("main")

        second = self.integrate()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already integrated", second.stdout)
        self.assertEqual(self.rev("main"), head)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.run_status(), "INTEGRATED")

    def test_run_merge_detects_a_manual_fast_forward(self):
        # Git integration succeeded by hand; the command still finalizes the
        # statuses without repeating the merge.
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.git("merge", "--ff-only", "josi/run/r1")
        self.assertEqual(self.rev("main"), self.rev("josi/run/r1"))

        result = self.integrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already integrated", result.stdout)
        self.assertIn("merged by hand", result.stdout)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.run_status(), "INTEGRATED")

    def test_run_merge_detects_a_manual_merge_after_branch_cleanup(self):
        # A user who merged the run by hand would reasonably remove the run
        # worktree and branch afterwards; the finalization must still work
        # from the recorded, verified final commit.
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.git("merge", "--ff-only", "josi/run/r1")
        self.git("worktree", "remove", str(self.run_worktree()))
        self.git("branch", "-d", "josi/run/r1")

        result = self.integrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already integrated", result.stdout)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.run_status(), "INTEGRATED")

    def test_run_merge_detects_a_manual_merge_commit(self):
        # A normal merge commit (for example after resolving conflicts by
        # hand) also counts as an integration of the recorded final commit.
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        (self.repo / "main-change.txt").write_text("main\n", encoding="utf-8")
        self.commit("main moves independently")
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "merge", "--no-ff", "--no-edit", "josi/run/r1",
        )
        self.git("merge-base", "--is-ancestor", self.rev("josi/run/r1"), "main")

        result = self.integrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already integrated", result.stdout)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.run_status(), "INTEGRATED")

    def test_non_fast_forward_merge_requires_manual_integration(self):
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        # The base branch diverges: a fast-forward is impossible.
        (self.repo / "base-moves.txt").write_text("base\n", encoding="utf-8")
        self.commit("base moves on")
        base_head = self.rev("main")
        self.assertNotEqual(base_head, self.rev("josi/run/r1"))

        result = self.integrate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manual Git integration", result.stderr)
        self.assertIn("git checkout main && git merge josi/run/r1", result.stdout)
        # Nothing changed: statuses, run, and branches are preserved.
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.run_status(), "COMPLETE")
        self.assertEqual(self.rev("main"), base_head)
        self.assertEqual(self.rev("josi/run/r1"), self.rev("josi/t1"))

        # After the user resolves the integration by hand, the same command
        # detects it and finalizes the statuses.
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "merge", "--no-edit", "josi/run/r1",
        )
        result = self.integrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.run_status(), "INTEGRATED")

    def test_run_merge_refuses_a_dirty_base_worktree(self):
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.dirty_base()

        result = self.integrate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncommitted or untracked changes", result.stdout)
        self.assertIn("manual Git integration", result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.run_status(), "COMPLETE")
        self.assertEqual(self.rev("main"), self.base_sha)

        self.clean_base()
        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.task_status(), "DONE")

    def test_run_merge_uses_the_configured_base_branch(self):
        self.git("branch", "develop")
        result = self.run_josi("init", "--base", "develop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)

        # ``develop`` is not checked out anywhere: josi integrates it through
        # a temporary merge worktree, and ``main`` never moves.
        result = self.integrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.rev("develop"), self.rev("josi/t1"))
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertEqual(
            list((self.repo / ".josi" / "integrate").glob("*")), [],
            "the temporary merge worktree must be removed",
        )

    def test_run_merge_updates_the_worktree_checking_out_the_base(self):
        self.git("branch", "develop")
        self.assertEqual(
            self.run_josi("init", "--base", "develop").returncode, 0
        )
        second = self.base / "develop-worktree"
        self.git("worktree", "add", str(second), "develop")
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)

        result = self.integrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.rev("HEAD", cwd=second),
            self.rev("josi/t1"),
            "the worktree with the base branch checked out is updated",
        )
        self.assertEqual(self.rev("main"), self.base_sha)

    def test_run_merge_refuses_when_the_run_branch_moved(self):
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        run_worktree = self.run_worktree()
        (run_worktree / "human.txt").write_text("human\n", encoding="utf-8")
        self.commit("human change on the run branch", cwd=run_worktree)
        moved = self.rev("josi/run/r1")
        final = self.run_show()["final_commit"]
        self.assertNotEqual(moved, final)

        result = self.integrate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("moved after the run finished", result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.run_status(), "COMPLETE")
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertEqual(self.rev("josi/run/r1"), moved)

    def test_run_merge_refuses_a_running_run(self):
        self.create("Add the feature")
        conn = tasks.connect(self.repo / ".josi" / "tasks.db")
        try:
            tasks.create_run(conn, "main", self.base_sha)
        finally:
            conn.close()

        result = self.integrate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still marked RUNNING", result.stderr)
        self.assertEqual(self.run_status(), "RUNNING")

    def test_ignored_local_file_is_never_overwritten_by_run_merge(self):
        # The base worktree ignores a local file that the staged task commit
        # starts tracking. Ignored files do not make the worktree dirty, so
        # only the merge itself can refuse instead of silently replacing the
        # human's local data.
        (self.repo / ".gitignore").write_text(
            ".josi/\n/josi.json\nlocal-notes.txt\n", encoding="utf-8"
        )
        self.commit("ignore the local notes")
        human = "local settings\n"
        (self.repo / "local-notes.txt").write_text(human, encoding="utf-8")
        base = self.rev("main")
        self.create("Track the local notes")
        self.env["JOSI_FAKE_RUN"] = "ignore-collision"
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "STAGED")

        result = self.integrate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local-notes.txt", result.stdout)
        self.assertIn("manual Git integration", result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.rev("main"), base, "main must not move")
        self.assertEqual(
            (self.repo / "local-notes.txt").read_text(encoding="utf-8"),
            human,
            "the ignored local file must never be silently overwritten",
        )

        # The human resolves the collision and the run integrates without
        # rerunning the worker, validation, or review.
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())
        (self.repo / "local-notes.txt").unlink()
        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.rev("main"), self.rev("josi/t1"))
        self.assertEqual(
            (self.repo / "local-notes.txt").read_text(encoding="utf-8"),
            "tracked default\n",
        )
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)

    def test_branch_squash_options_cannot_fake_a_run_merge(self):
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())
        # A branch-level option turns a plain merge into a squash that only
        # stages the changes; josi must still fast-forward and may report DONE
        # only once the approved commit is really in the base branch.
        self.git("config", "branch.main.mergeOptions", "--squash")

        result = self.integrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        approved = self.rev("josi/t1")
        self.assertEqual(self.rev("main"), approved)
        self.git("merge-base", "--is-ancestor", approved, "main")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)

    def test_staged_task_is_not_executed_again_until_done(self):
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())
        head = self.rev("josi/t1")

        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Task t1 is STAGED in run r1", result.stderr)
        self.assertIn("josi run merge r1", result.stderr)
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)
        self.assertEqual(self.rev("josi/t1"), head)
        self.assertEqual(self.rev("main"), self.base_sha)

    def test_approved_task_is_staged_without_rerunning_agents(self):
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())

        # Simulate an interruption between the approval and the staging
        # status: the run is still RUNNING and the task is APPROVED. The
        # retry resumes the same run and stages the recorded commit.
        self.set_task_status("APPROVED")
        conn = tasks.connect(self.repo / ".josi" / "tasks.db")
        try:
            tasks.set_run_status(conn, 1, tasks.RUN_RUNNING)
        finally:
            conn.close()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.rev("josi/run/r1"), self.rev("josi/t1"))
        self.assertEqual(self.run_status(), "COMPLETE")
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)

    def test_interrupted_staging_is_recorded_on_resume(self):
        # A crash between the staging fast-forward and the STAGED record
        # leaves the run branch exactly at the task's approved commit. The
        # retry recognizes that verified commit, records STAGED, and reruns
        # no agent; the expected tip moves with the record.
        self.create("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())
        conn = tasks.connect(self.repo / ".josi" / "tasks.db")
        try:
            tasks.set_task_status(conn, 1, tasks.STATUS_APPROVED)
            conn.execute(
                "UPDATE runs SET expected_commit = ?, status = ? WHERE id = 1",
                (self.base_sha, tasks.RUN_RUNNING),
            )
            conn.commit()
        finally:
            conn.close()
        approved = self.rev("josi/t1")
        self.assertEqual(self.rev("josi/run/r1"), approved)

        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recording STAGED", result.stdout)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.run_status("r1"), "COMPLETE")
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)
        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.task_status(), "DONE")

    def test_interrupted_run_after_staging_is_resumed_and_finished(self):
        # A crash between staging t1 and finishing the run leaves the run
        # RUNNING; the next run continues it, notices the task is already
        # staged, and completes the run without touching any agent.
        self.create("First")
        self.assertEqual(self.run_task("t1").returncode, 0)
        conn = tasks.connect(self.repo / ".josi" / "tasks.db")
        try:
            tasks.set_run_status(conn, 1, tasks.RUN_RUNNING)
        finally:
            conn.close()
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())

        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already STAGED in run r1", result.stdout)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.run_status(), "COMPLETE")
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)

        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.task_status(), "DONE")


class DependencySchedulingTests(IntegrationTestCase):
    def test_staged_dependency_is_satisfied_in_the_same_run(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        result = self.run_task(t1, t2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(t1), "STAGED")
        self.assertEqual(self.task_status(t2), "STAGED")
        self.assertEqual(self.rev("josi/t2~1"), self.rev("josi/t1"))
        self.assertNotIn("STAGED in run", result.stderr)

    def test_staged_dependency_in_another_run_blocks_execution(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        self.assertEqual(self.run_task(t1).returncode, 0)
        self.assertEqual(self.task_status(t1), "STAGED")

        # t1 is staged in r1, not DONE: a new run must not start t2, and the
        # refusal names the run and the exact command that fixes it.
        result = self.run_task(t2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr,
            "josi: error: "
            "Cannot execute task t2.\n"
            "\n"
            "Dependency t1 is STAGED in run r1 and must be\n"
            "marked DONE before this task can execute.\n"
            "\n"
            "Run:\n"
            "    josi run merge r1\n"
            "\n"
            "If you already merged r1 manually, execute the\n"
            "same command to finalize its status.\n",
        )
        self.assertEqual(self.task_status(t2), "PENDING")
        self.assertNotIn("t2", self.worker_cwds())

        # After integration the dependency is DONE and the dependent runs in
        # its own, new run.
        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.task_status(t1), "DONE")
        result = self.run_task(t2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(t2), "STAGED")
        self.assertEqual(self.run_status("r2"), "COMPLETE")

    def test_approved_dependency_does_not_satisfy_dependents(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        self.set_task_status("APPROVED", 1)

        result = self.run_task(t2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("depends on t1 (APPROVED)", result.stderr)
        self.assertEqual(self.task_status(t2), "PENDING")
        self.assertFalse(self.worktree(t2).exists())

    def test_unfinished_dependencies_refuse_execution(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        result = self.run_task(t2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("depends on t1 (PENDING)", result.stderr)
        self.assertIn("DONE or STAGED in the current run", result.stderr)
        self.assertEqual(self.worker_calls(), [])
        self.assertEqual(self.task_status(t2), "PENDING")

    def test_explicit_task_list_runs_strictly_in_order(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        t3 = self.create("Third", "--depends-on", t2)
        result = self.run_task(t1, t2, t3)
        self.assertEqual(result.returncode, 0, result.stderr)
        for task_id in (t1, t2, t3):
            self.assertEqual(self.task_status(task_id), "STAGED")
        self.assertEqual(self.worker_cwds(), [t1, t2, t3])
        self.assertEqual(self.rev("josi/run/r1"), self.rev("josi/t3"))
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertIn("Summary: completed: t1, t2, t3", result.stdout)

    def test_explicit_task_list_stops_at_the_first_failure(self):
        self.env["JOSI_FAKE_REVIEW_JSON"] = json.dumps(MAJOR)
        t1 = self.create("First")
        t2 = self.create("Second")
        result = self.run_task(t1, t2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(t1), "NEEDS_HUMAN")
        self.assertEqual(self.task_status(t2), "PENDING")
        self.assertNotIn("t2", self.worker_cwds(), "t2 must not run")
        self.assertIn("stopped at t1", result.stdout)
        self.assertIn("Summary:", result.stdout)
        # The stopped batch leaves its run open and resumable; a partly
        # finished plan is never eligible for integration.
        self.assertEqual(self.run_status(), "RUNNING")
        refused = self.integrate()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("still marked RUNNING", refused.stderr)

    def test_all_runs_in_dependency_order(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        t3 = self.create("Third", "--depends-on", t1)
        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        for task_id in (t1, t2, t3):
            self.assertEqual(self.task_status(task_id), "STAGED")
        self.assertEqual(self.worker_cwds(), [t1, t2, t3])
        self.assertEqual(self.rev("josi/run/r1"), self.rev("josi/t3"))
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertIn("Summary: completed: t1, t2, t3", result.stdout)

    def test_all_executes_independent_tasks_in_id_order(self):
        self.create("First")
        self.create("Second")
        self.create("Third")
        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.worker_cwds(), ["t1", "t2", "t3"])

    def test_all_stops_when_a_task_needs_human(self):
        self.env["JOSI_FAKE_REVIEW_JSON"] = json.dumps(MAJOR)
        t1 = self.create("First")
        t2 = self.create("Second")
        result = self.run_task("--all")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(t1), "NEEDS_HUMAN")
        self.assertEqual(self.task_status(t2), "PENDING")
        self.assertNotIn("t2", self.worker_cwds(), "t2 must not run")
        self.assertIn("stopped at t1", result.stdout)

    def test_all_never_executes_tasks_with_unfinished_dependencies(self):
        self.env["JOSI_FAKE_REVIEW_JSON"] = json.dumps(MAJOR)
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
        # The blocked batch resumes the still-open run instead of opening a
        # second one.
        self.assertEqual(self.run_status("r1"), "RUNNING")

    def test_all_reports_the_actionable_message_for_a_staged_dependency(self):
        t1 = self.create("First")
        t2 = self.create("Second", "--depends-on", t1)
        self.assertEqual(self.run_task(t1).returncode, 0)
        workers = len(self.worker_calls())

        result = self.run_task("--all")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Dependency t1 is STAGED in run r1", result.stderr)
        self.assertIn("josi run merge r1", result.stderr)
        self.assertEqual(
            len(self.worker_calls()), workers, "t2 must never execute"
        )

    def test_all_reports_tasks_that_already_need_a_human(self):
        self.env["JOSI_FAKE_REVIEW_JSON"] = json.dumps(MAJOR)
        self.create("First")
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status("t1"), "NEEDS_HUMAN")
        workers = len(self.worker_calls())

        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Summary: completed: none", result.stdout)
        self.assertIn("needs human intervention: t1", result.stdout)
        self.assertEqual(len(self.worker_calls()), workers, "no retry")
        # The run stays open: its selected task is not staged, so it is
        # never reported as finished or integrated.
        self.assertEqual(self.run_status("r1"), "RUNNING")
        self.assertIn("still open", result.stdout)
        refused = self.integrate()
        self.assertNotEqual(refused.returncode, 0)

    def test_all_with_nothing_to_run_succeeds_without_a_run(self):
        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Summary: completed: none", result.stdout)
        self.assertEqual(self.worker_calls(), [])
        run_list = self.run_josi("run", "list")
        self.assertEqual(run_list.stdout, "", "no empty run is created")

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
        lock_path = self.repo / ".josi" / "execution.lock"
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
        self.env["JOSI_FAKE_RUN_MARKER"] = str(marker)
        self.env["JOSI_FAKE_RUN_COUNT_FILE"] = str(self.base / "worker-count")
        self.env["JOSI_FAKE_RUN_SEQUENCE"] = json.dumps(
            [{"wait_for": str(release)}]
        )

        parent = subprocess.Popen(
            [sys.executable, "-m", "josi", "task", "run", "--all"],
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
        self.assertEqual(self.task_status("t1"), "STAGED")
        self.assertEqual(self.task_status("t2"), "STAGED")
        self.assertEqual(self.rev("josi/run/r1"), self.rev("josi/t2"))
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertIn("Summary: completed: t1, t2", out)


class IntegrationOutputTests(IntegrationTestCase):
    def test_terminal_shows_staging_events_and_the_summary(self):
        self.create("First")
        self.create("Second", "--depends-on", "t1")
        result = self.run_task("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        stdout = result.stdout
        self.assertLess(
            stdout.index("Review approved"),
            stdout.index("Fast-forward merging into run r1..."),
        )
        self.assertLess(
            stdout.index("Fast-forward merging into run r1..."),
            stdout.index("Task t1 \u2192 STAGED (run r1)"),
        )
        self.assertLess(
            stdout.index("Task t1 \u2192 STAGED (run r1)"),
            stdout.index("task t2:"),
        )
        self.assertIn("Summary: completed: t1, t2", stdout)
        self.assertIn("integrate it with 'josi run merge r1'", stdout)
        self.assertNotIn("fake opencode:", stdout)
        self.assertNotIn("fake opencode:", result.stderr)

    def test_logs_and_worktrees_are_preserved_after_staging(self):
        self.create("First")
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertTrue(self.log_paths("t1"))
        log = self.log_text("t1")
        self.assertIn("result: IMPLEMENTED", log)
        self.assertTrue(self.worktree().is_dir())
        self.assertTrue(self.run_worktree().is_dir())


if __name__ == "__main__":
    unittest.main()
