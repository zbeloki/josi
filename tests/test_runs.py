"""End-to-end tests for actl Stage 4 task execution.

Every test runs the CLI as a subprocess against a disposable Git repository
and the fake ``opencode`` executable from ``test_actl``; the fake emulates one
noninteractive worker turn (it can commit, leave a dirty worktree, fail, or
wait for the test to release it), so no real OpenCode process or model call is
needed. The project also configures a reviewer model, so a successful worker
run continues through validation and the fake approving reviewer; these tests
assert the worker stage and its outcomes, while Stage 5 behaviour is covered
in ``test_verification``.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path

from test_actl import ActlTestCase


@unittest.skipUnless(shutil.which("git"), "git is required")
class TaskRunTestCase(ActlTestCase):
    """An initialized project on a ``main`` branch with one base commit."""

    def setUp(self):
        super().setUp()
        self.init_git()
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        # Track ``.gitignore`` in the base commit (init would add the entry
        # itself) so the checkout stays clean for the assertions below.
        (self.repo / ".gitignore").write_text(".actl/\n", encoding="utf-8")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.commit("base commit")
        # Initialize after the base commit, so ``init`` detects and stores
        # ``main`` instead of the unborn HEAD of an empty repository.
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.base_sha = self.git("rev-parse", "main").stdout.strip()
        result = self.run_actl("init", "--worker-model", "provider/model")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_actl("init", "--reviewer-model", "reviewer/model")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_actl(
            "init", "--escalation-worker-model", "escalation/model"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    # -- helpers ---------------------------------------------------------

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(
            ["git", *args], cwd=str(cwd or self.repo), env=self.env,
            capture_output=True, text=True, check=check,
        )

    def commit(self, message, cwd=None):
        self.git("add", "-A", cwd=cwd)
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-m", message, cwd=cwd,
        )

    def run_task(self, *args):
        return self.run_actl("task", "run", *args)

    def make_task(self, title="Add the feature", *extra):
        result = self.run_actl(
            "task", "create", "--title", title, "--description", "Do the thing.",
            *extra,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return "t1"

    def task_status(self, task_id="t1"):
        result = self.run_actl("task", "show", task_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in result.stdout.splitlines():
            if line.startswith("status: "):
                return line.split(": ", 1)[1]
        self.fail("no status in:\n" + result.stdout)

    def run_status(self, run_id="r1"):
        result = self.run_actl("run", "show", run_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in result.stdout.splitlines():
            if line.startswith("status: "):
                return line.split(": ", 1)[1]
        self.fail("no status in:\n" + result.stdout)

    def integrate(self, run_id="r1"):
        """Run ``actl run merge`` for ``run_id``."""
        return self.run_actl("run", "merge", run_id)

    def worktree(self, task_id="t1") -> Path:
        return self.repo / ".actl" / "worktrees" / task_id

    def run_worktree(self, run_id="r1") -> Path:
        return self.repo / ".actl" / "run-worktrees" / run_id

    def rewind_run_to_base(self, run_id="r1"):
        """Reset a run branch to its start commit, as if staging never ran.

        Only tests use this to simulate an execution that was interrupted
        before a task was staged into the run; the recorded expected commit
        is reset with it, exactly as if the staging transaction never ran.
        """
        self.git("reset", "--hard", self.base_sha, cwd=self.run_worktree(run_id))
        conn = sqlite3.connect(str(self.repo / ".actl" / "tasks.db"))
        try:
            conn.execute(
                "UPDATE runs SET expected_commit = ? WHERE id = ?",
                (self.base_sha, int(run_id.lstrip("r"))),
            )
            conn.commit()
        finally:
            conn.close()

    def log_paths(self, task_id="t1"):
        return [
            path
            for path in sorted(
                (self.repo / ".actl" / "runs").glob(task_id + "-*.log")
            )
            if "-validation" not in path.name and "-review" not in path.name
            and "-tests" not in path.name
        ]

    def log_text(self, task_id="t1"):
        paths = self.log_paths(task_id)
        self.assertTrue(paths, "no run log was written")
        return paths[-1].read_text(encoding="utf-8")

    @staticmethod
    def agent_of(call):
        argv = call["argv"]
        return argv[argv.index("--agent") + 1] if "--agent" in argv else ""

    def worker_calls(self):
        # The test writer also uses the build agent, but answers with JSON
        # output; the worker turns are the plain transcript ones.
        return [
            call for call in self.calls()
            if call["argv"][:1] == ["run"] and self.agent_of(call) == "build"
            and "--format" not in call["argv"]
        ]

    def reviewer_calls(self):
        return [
            call for call in self.calls()
            if call["argv"][:1] == ["run"] and self.agent_of(call) == "plan"
        ]

    def generation_calls(self):
        """The test-generation turns: the build agent answering with JSON."""
        return [
            call for call in self.calls()
            if call["argv"][:1] == ["run"] and self.agent_of(call) == "build"
            and "--format" in call["argv"]
        ]

    def worker_prompt(self):
        calls = self.worker_calls()
        self.assertTrue(calls, "the worker was never invoked")
        return calls[-1]["argv"][-1]

    def rev(self, ref, cwd=None):
        return self.git("rev-parse", ref, cwd=cwd).stdout.strip()

    def wait_for_path(self, path, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                return True
            time.sleep(0.05)
        return False

    def wait_for_lock_release(self, timeout=30):
        lock_path = self.repo / ".actl" / "execution.lock"
        deadline = time.time() + timeout
        while time.time() < deadline:
            handle = open(lock_path, "a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                time.sleep(0.05)
            finally:
                handle.close()
        return False

    def kill_pid(self, path):
        if path.exists():
            pid = int(path.read_text(encoding="utf-8").strip())
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, ValueError):
                pass


class RunExecutionTests(TaskRunTestCase):
    def test_successful_run_builds_branch_worktree_and_commits(self):
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Task status: IMPLEMENTED", result.stdout)
        self.assertIn("Task t1 \u2192 STAGED (run r1)", result.stdout)
        self.assertIn("log:", result.stdout)

        worktree = self.worktree()
        self.assertTrue(worktree.is_dir())
        self.assertEqual(self.rev("HEAD", cwd=worktree), self.rev("actl/t1"))
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree).stdout.strip(),
            "actl/t1",
        )
        self.assertEqual(self.git("status", "--porcelain", cwd=worktree).stdout, "")

        subjects = self.git("log", "--format=%s", "actl/t1").stdout.splitlines()
        self.assertEqual(subjects[0], "worker: implement task")
        self.assertIn("base commit", subjects)

        # The task is STAGED in its run branch; the base branch never moved.
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.rev("actl/run/r1"), self.rev("actl/t1"))
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertTrue(self.run_worktree().is_dir())
        self.assertEqual(
            self.git(
                "rev-parse", "--abbrev-ref", "HEAD", cwd=self.run_worktree()
            ).stdout.strip(),
            "actl/run/r1",
        )
        self.assertEqual(self.run_status(), "COMPLETE")

        log = self.log_text()
        self.assertTrue(log.startswith("actl task run\n"), log[:80])
        self.assertIn("branch: actl/t1\n", log)
        self.assertIn("worktree: " + str(worktree) + "\n", log)
        self.assertIn("session: ", log)
        self.assertLess(
            log.index("actl task run"),
            log.index("fake opencode: worker turn finished"),
            "the log header must come before the worker transcript",
        )
        self.assertLess(
            log.index("fake opencode: worker turn finished"),
            log.index("result: IMPLEMENTED"),
            "the log footer must come after the worker transcript",
        )

    def test_branch_starts_from_the_latest_local_main(self):
        self.make_task()
        (self.repo / "later.txt").write_text("later\n", encoding="utf-8")
        self.commit("later commit")
        later = self.rev("main")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        # The run branch starts at the latest local base commit and the task
        # branch continues from the run branch; after staging, the run branch
        # points at the approved task commit.
        self.assertEqual(self.rev("actl/t1~1"), later)
        self.assertEqual(self.rev("actl/run/r1"), self.rev("actl/t1"))
        self.assertEqual(self.rev("main"), later)

    def test_task_branch_starts_from_the_configured_base_branch(self):
        # Configure ``develop`` as the base, then let ``main`` advance: the
        # run branch must start from ``develop``, never from a hardcoded or
        # newer ``main``.
        self.git("branch", "develop")
        result = self.run_actl("init", "--base", "develop")
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.repo / "later.txt").write_text("later\n", encoding="utf-8")
        self.commit("later commit on main")
        later = self.rev("main")

        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rev("actl/t1~1"), self.base_sha)
        self.assertEqual(self.rev("actl/run/r1"), self.rev("actl/t1"))
        self.assertNotEqual(self.rev("main"), self.rev("develop"))
        self.assertIn("Never merge into actl/run/r1.", self.worker_prompt())
        self.assertEqual(self.rev("develop"), self.base_sha, "develop stays put")

        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.rev("develop"), self.rev("actl/t1"))
        self.assertEqual(self.rev("main"), later)

    def test_base_branch_stays_untouched_during_execution(self):
        self.make_task()
        main_before = self.rev("main")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Execution only stages into the run branch; the base branch moves
        # exactly once, when the run is integrated explicitly.
        self.assertEqual(self.rev("main"), main_before)
        self.assertNotEqual(self.rev("actl/run/r1"), main_before)
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main"
        )
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.rev("main"), self.rev("actl/t1"))
        self.git("merge-base", "--is-ancestor", main_before, "main")
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main"
        )
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_worktree_is_created_only_when_the_task_runs(self):
        self.make_task()
        self.assertFalse((self.repo / ".actl" / "worktrees").exists())
        self.assertEqual(self.worker_calls(), [])

        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertTrue(self.worktree().is_dir())

    def test_worker_receives_title_description_and_model(self):
        self.assertEqual(
            self.run_actl(
                "init", "--worker-model", "openrouter/test-model#high"
            ).returncode,
            0,
        )
        self.make_task("Add the login form")
        self.run_actl(
            "task", "update", "t1", "--description", "First line.\nSecond line."
        )
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)

        prompt = self.worker_prompt()
        self.assertIn("Add the login form", prompt)
        self.assertIn("First line.", prompt)
        self.assertIn("Second line.", prompt)
        self.assertIn("commit", prompt.lower())

        call = self.worker_calls()[-1]
        self.assertIn("openrouter/test-model#high", call["argv"])
        self.assertIn("--session", call["argv"])
        self.assertEqual(call["cwd"], str(self.worktree()))

    def test_run_from_a_subdirectory_uses_the_project_root(self):
        self.make_task()
        sub = self.repo / "src" / "nested"
        sub.mkdir(parents=True)
        result = self.run_actl("task", "run", "t1", cwd=sub)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.worktree().is_dir())
        self.assertNotEqual(self.rev("actl/t1"), self.base_sha)

    def test_run_requires_an_initialized_project(self):
        repo = self.base / "plain"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(repo)], check=True, capture_output=True
        )
        result = self.run_actl("task", "run", "t1", cwd=repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("actl init", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_unknown_task_is_reported(self):
        result = self.run_task("t9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("task t9 does not exist", result.stderr)
        self.assertEqual(self.worker_calls(), [])


class TerminalOutputTests(TaskRunTestCase):
    """The terminal shows actl events; the worker transcript stays in the log."""

    def test_terminal_reports_events_but_not_worker_output(self):
        self.make_task("Add the login form")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)

        stdout = result.stdout
        self.assertIn("task t1: Add the login form", stdout)
        self.assertIn("model: provider/model", stdout)
        self.assertIn("Preparing worktree", stdout)
        self.assertIn("Starting worker", stdout)
        self.assertIn("Worker finished with exit code 0", stdout)
        self.assertIn("Task status: IMPLEMENTED", stdout)
        # Events carry timestamps, and the log path is printed when the
        # worker starts, before the worker-finished event.
        self.assertRegex(
            stdout,
            r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\]"
            r" Preparing worktree",
        )
        # The worker's own log line ("... log: ...", not the test writer's
        # "... tests log: ...") sits between the worker start and its finish.
        worker_log = stdout.find("] log: ")
        self.assertGreater(worker_log, 0)
        self.assertLess(stdout.index("Starting worker"), worker_log)
        self.assertLess(worker_log, stdout.index("Worker finished"))

        # The worker transcript never reaches the terminal.
        self.assertNotIn("fake opencode:", stdout)
        self.assertNotIn("fake opencode:", result.stderr)

    def test_failed_run_reports_events_and_status(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)

        self.assertIn("Worker failed with exit code 1", result.stdout)
        self.assertIn("Task status: FAILED", result.stdout)
        self.assertNotIn("fake opencode:", result.stdout)
        self.assertNotIn("fake opencode:", result.stderr)
        self.assertIn("failed", result.stderr)
        self.assertIn("log:", result.stderr)

    def test_preparation_failure_reports_status_without_starting_the_worker(self):
        self.make_task()
        self.git("branch", "-m", "main", "trunk")
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)

        # The run cannot start without its base branch: the refusal happens
        # before any worktree or worker, and the task stays PENDING.
        self.assertNotIn("Starting worker", result.stdout)
        self.assertIn("'main' does not exist", result.stderr)
        self.assertIn("cannot start a run", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertFalse((self.repo / ".actl" / "run-worktrees").exists())

    def test_log_keeps_the_complete_worker_output(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)

        log = self.log_text()
        self.assertIn("fake opencode: worker turn finished", log)
        self.assertIn("fake opencode: worker detail", log)
        self.assertIn("exit_code: 1", log)
        self.assertIn("result: FAILED", log)

    def test_log_is_written_while_the_worker_runs(self):
        self.make_task()
        release = self.base / "worker.release"
        self.env["ACTL_FAKE_RUN_STREAM"] = "live"
        self.env["ACTL_FAKE_RUN_WAIT_FOR"] = str(release)

        parent = subprocess.Popen(
            [sys.executable, "-m", "actl", "task", "run", "t1"],
            cwd=str(self.repo), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            content = ""
            deadline = time.time() + 30
            while time.time() < deadline:
                paths = self.log_paths()
                if paths:
                    content = paths[-1].read_text(
                        encoding="utf-8", errors="replace"
                    )
                    if "live on stderr" in content:
                        break
                time.sleep(0.05)
            # Both worker streams reached the log before it finished.
            self.assertIn("live on stdout", content)
            self.assertIn("live on stderr", content)
            self.assertIsNone(parent.poll(), "actl finished unexpectedly early")
            self.assertFalse(
                release.exists(), "the worker must still be running"
            )
        finally:
            release.write_text("go", encoding="utf-8")
            try:
                out, err = parent.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                parent.kill()
                out, err = parent.communicate()

        self.assertEqual(parent.returncode, 0, err.decode("utf-8", "replace"))
        self.assertNotIn(b"fake opencode:", out)
        self.assertNotIn(b"fake opencode:", err)
        log = self.log_text()
        self.assertIn("live on stdout", log)
        self.assertIn("live on stderr", log)
        self.assertIn("fake opencode: worker turn finished", log)
        self.assertIn("result: IMPLEMENTED", log)


class WorkerModelTests(TaskRunTestCase):
    def test_worker_model_is_required(self):
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        del metadata["worker_model"]
        self.metadata_path().write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no worker model", result.stderr)
        self.assertEqual(self.worker_calls(), [])
        self.assertFalse((self.repo / ".actl" / "worktrees").exists())

    def test_invalid_worker_model_is_rejected(self):
        for value in ("", "model", "provider/", "/model", "two words/model"):
            with self.subTest(value=value):
                result = self.run_actl("init", "--worker-model", value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid worker model", result.stderr)
        # A leading dash would be read as an option; with the "=" form it
        # reaches actl and is rejected as invalid.
        result = self.run_actl("init", "--worker-model=-flag/model")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid worker model", result.stderr)
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(metadata["worker_model"], "provider/model")

    def test_worker_model_is_stored_in_project_metadata(self):
        created_at = json.loads(
            self.metadata_path().read_text(encoding="utf-8")
        )["created_at"]
        result = self.run_actl("init", "--worker-model", "provider/other")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the worker model", result.stdout)

        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(metadata["worker_model"], "provider/other")
        self.assertEqual(
            metadata["created_at"], created_at, "metadata must be preserved"
        )
        self.assertEqual(metadata["name"], "repo")

        # Repeating the same model leaves the file alone.
        before = self.metadata_path().read_bytes()
        self.assertEqual(
            self.run_actl("init", "--worker-model", "provider/other").returncode, 0
        )
        self.assertEqual(self.metadata_path().read_bytes(), before)

        # A plain init never removes the configured model.
        self.assertEqual(self.run_actl("init").returncode, 0)
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(metadata["worker_model"], "provider/other")

    def test_model_can_be_set_during_the_first_init(self):
        repo = self.base / "fresh"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(repo)], check=True, capture_output=True
        )
        result = self.run_actl(
            "init", "--worker-model", "provider/model", cwd=repo
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(
            (repo / ".actl" / "project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["worker_model"], "provider/model")


class RunFailureTests(TaskRunTestCase):
    def test_failed_worker_preserves_worktree_and_log(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed", result.stderr)
        self.assertIn("log:", result.stderr)

        self.assertEqual(self.task_status(), "FAILED")
        worktree = self.worktree()
        self.assertTrue(worktree.is_dir(), "the worktree must be preserved")
        subjects = self.git("log", "--format=%s", "actl/t1").stdout.splitlines()
        self.assertEqual(subjects[0], "worker: implement task")
        self.assertEqual(self.git("status", "--porcelain", cwd=worktree).stdout, "")
        self.assertEqual(self.rev("main"), self.base_sha)

        log = self.log_text()
        self.assertIn("exit_code: 1", log)
        self.assertIn("result: FAILED", log)
        self.assertIn("fake opencode: worker turn finished", log)
        self.assertIn("fake opencode: worker detail", log)

    def test_dirty_worktree_fails_the_run(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN"] = "dirty"
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty", result.stderr)

        self.assertEqual(self.task_status(), "FAILED")
        worktree = self.worktree()
        self.assertTrue((worktree / "worker-output.txt").is_file())
        self.assertIn("result: FAILED", self.log_text())

    def test_worker_without_committed_changes_fails_the_run(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN"] = "none"
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no committed changes", result.stderr)

        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(self.rev("actl/t1"), self.base_sha)
        self.assertEqual(
            self.git("status", "--porcelain", cwd=self.worktree()).stdout, ""
        )
        self.assertIn("result: FAILED", self.log_text())

    def test_worker_with_only_an_empty_commit_fails_the_run(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN"] = "empty"
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no committed changes", result.stderr)

        self.assertEqual(self.task_status(), "FAILED")
        self.assertNotEqual(self.rev("actl/t1"), self.base_sha)
        self.assertEqual(
            self.git("status", "--porcelain", cwd=self.worktree()).stdout, ""
        )

    def test_retry_accepts_previously_committed_work_without_a_new_commit(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        first_head = self.rev("actl/t1")
        self.assertEqual(self.task_status(), "FAILED")

        # The retry succeeds from the existing committed work even when the
        # worker has nothing new to commit.
        self.env["ACTL_FAKE_RUN_EXIT"] = "0"
        self.env["ACTL_FAKE_RUN"] = "none"
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.rev("actl/t1"), first_head)
        self.assertIn("result: IMPLEMENTED", self.log_text())

    def test_worker_leaving_another_branch_fails_the_run(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN"] = "detach"
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("instead of", result.stderr)
        self.assertEqual(self.task_status(), "FAILED")

    def test_staged_task_is_not_run_again(self):
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        head = self.rev("actl/t1")
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())

        # A STAGED task is already part of its run branch; it must be marked
        # DONE through 'actl run merge' before it can execute again.
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot execute task t1.", result.stderr)
        self.assertIn("Task t1 is STAGED in run r1", result.stderr)
        self.assertIn("actl run merge r1", result.stderr)
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)
        self.assertEqual(self.rev("actl/t1"), head)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.rev("main"), self.base_sha)

    def test_done_task_is_not_run_again(self):
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.integrate().returncode, 0)
        head = self.rev("actl/t1")
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())

        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already DONE", result.stderr)
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)
        self.assertEqual(self.rev("actl/t1"), head)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.rev("main"), head)

    def test_retry_reuses_existing_worktree_and_work(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        first_head = self.rev("actl/t1")
        self.assertEqual(self.task_status(), "FAILED")

        self.env["ACTL_FAKE_RUN_EXIT"] = "0"
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reusing worktree", result.stdout)

        self.assertEqual(self.task_status(), "STAGED")
        second_head = self.rev("actl/t1")
        self.assertNotEqual(second_head, first_head)
        # The first attempt's commit is still an ancestor: nothing was reset.
        self.git("merge-base", "--is-ancestor", first_head, second_head)
        subjects = self.git("log", "--format=%s", "actl/t1").stdout.splitlines()
        self.assertEqual(subjects.count("worker: implement task"), 2)

    def test_missing_main_branch_is_reported(self):
        self.make_task()
        self.git("branch", "-m", "main", "trunk")
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'main' does not exist", result.stderr)
        self.assertIn("git branch main", result.stderr)
        self.assertIn("cannot start a run", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertFalse(self.worktree().exists())

    def test_retry_refuses_when_the_base_branch_was_removed(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        before = self.rev("actl/t1")
        self.assertEqual(len(self.worker_calls()), 1)

        # The base disappears between attempts: the retry must fail before
        # launching another worker, not after the run. The failed batch left
        # its run open, so the retry resumes it and validates the base first.
        self.git("checkout", "-b", "feature")
        self.git("branch", "-D", "main")
        self.env["ACTL_FAKE_RUN_EXIT"] = "0"
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'main' does not exist", result.stderr)
        self.assertEqual(len(self.worker_calls()), 1, "no worker may be launched")
        self.assertEqual(self.rev("actl/t1"), before)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(self.run_status("r1"), "RUNNING")

    def test_invalid_base_branch_in_metadata_is_rejected(self):
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        metadata["base_branch"] = "main^"
        self.metadata_path().write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid 'base_branch'", result.stderr)
        self.assertEqual(self.worker_calls(), [])
        self.assertFalse(self.worktree().exists())

    def test_first_run_refuses_a_preexisting_branch(self):
        self.make_task()
        self.git("branch", "actl/t1", "main")
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("actl/t1 already exists", result.stderr)
        self.assertEqual(self.worker_calls(), [])
        self.assertFalse(self.worktree().exists())

    def test_first_run_refuses_an_unregistered_worktree_directory(self):
        self.make_task()
        (self.repo / ".actl" / "worktrees" / "t1").mkdir(parents=True)
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a registered Git worktree", result.stderr)
        self.assertEqual(self.worker_calls(), [])

    def test_retry_reattaches_a_removed_worktree(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        first_head = self.rev("actl/t1")

        self.git("worktree", "remove", "--force", str(self.worktree()))
        self.assertFalse(self.worktree().exists())

        self.env["ACTL_FAKE_RUN_EXIT"] = "0"
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.worktree().is_dir())
        self.assertEqual(self.task_status(), "STAGED")
        # The earlier commit is still there: the branch was re-attached, not
        # recreated.
        self.git("merge-base", "--is-ancestor", first_head, "actl/t1")

    def test_worktree_deleted_by_hand_is_reported_then_recoverable(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        first_head = self.rev("actl/t1")

        # Deleting the directory without unregistering the worktree leaves a
        # prunable registration behind.
        shutil.rmtree(self.worktree())

        self.env["ACTL_FAKE_RUN_EXIT"] = "0"
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("git worktree prune", result.stderr)
        self.assertEqual(self.task_status(), "FAILED")

        self.git("worktree", "prune")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.worktree().is_dir())
        self.git("merge-base", "--is-ancestor", first_head, "actl/t1")


class LegacyProjectTests(TaskRunTestCase):
    """Projects initialized before ``base_branch`` existed keep working."""

    def strip_base_branch(self):
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        del metadata["base_branch"]
        self.metadata_path().write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        return self.metadata_path().read_bytes()

    def test_legacy_project_without_base_branch_uses_main(self):
        before = self.strip_base_branch()
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Using detected base branch 'main'", result.stdout)
        self.assertEqual(self.rev("actl/t1~1"), self.base_sha)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.rev("main"), self.base_sha)
        # Detection never rewrites the existing configuration by itself.
        self.assertEqual(self.metadata_path().read_bytes(), before)

    def test_reinit_adds_the_base_branch_to_a_legacy_project(self):
        self.strip_base_branch()
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the base branch to main", result.stdout)
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(metadata["base_branch"], "main")
        self.assertEqual(metadata["worker_model"], "provider/model")
        self.assertEqual(metadata["name"], "repo")

    def test_reinit_keeps_an_existing_base_branch(self):
        before = self.metadata_path().read_bytes()
        # ``main`` plus ``master`` is ambiguous, but a stored selection wins
        # and a plain init never changes it.
        self.git("branch", "master")
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already initialized", result.stdout)
        self.assertEqual(self.metadata_path().read_bytes(), before)


@unittest.skipUnless(shutil.which("git"), "git is required")
class EmptyRepositoryTests(ActlTestCase):
    """A repository without commits: plan and tasks work, runs refuse."""

    def setUp(self):
        super().setUp()
        self.init_git()
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        unborn = self.git("symbolic-ref", "--short", "HEAD").stdout.strip()
        self.assertEqual(metadata["base_branch"], unborn)
        result = self.run_actl("init", "--worker-model", "provider/model")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_actl("init", "--reviewer-model", "reviewer/model")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_actl(
            "init", "--escalation-worker-model", "escalation/model"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(
            ["git", *args], cwd=str(cwd or self.repo), env=self.env,
            capture_output=True, text=True, check=check,
        )

    def make_task(self):
        result = self.run_actl(
            "task", "create", "--title", "First task",
            "--description", "Do the thing.",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def task_status(self):
        result = self.run_actl("task", "show", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in result.stdout.splitlines():
            if line.startswith("status: "):
                return line.split(": ", 1)[1]
        self.fail("no status in:\n" + result.stdout)

    def commit_initial(self):
        (self.repo / "README.md").write_text("initial\n", encoding="utf-8")
        self.git("add", "-A")
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-m", "Initial commit",
        )

    def test_initialization_never_creates_a_commit(self):
        self.assertNotEqual(
            self.git("rev-parse", "--verify", "HEAD", check=False).returncode, 0
        )

    def test_plan_and_task_management_work_without_commits(self):
        self.make_task()
        listing = self.run_actl("task", "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("t1  PENDING  First task", listing.stdout)

        self.env["ACTL_FAKE_SESSION_ID"] = "ses_fakeempty0000000000000001"
        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Started planner session", result.stdout)

    def test_task_run_refuses_until_the_base_has_a_commit(self):
        self.make_task()
        result = self.run_actl("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no commits yet", result.stderr)
        self.assertIn("git commit --allow-empty", result.stderr)
        self.assertEqual(self.calls(), [], "no worker session may be created")
        self.assertFalse((self.repo / ".actl" / "worktrees").exists())
        self.assertFalse((self.repo / ".actl" / "run-worktrees").exists())
        self.assertEqual(self.task_status(), "PENDING")

    def test_run_refuses_an_unborn_configured_base_with_guidance(self):
        self.make_task()
        result = self.run_actl("init", "--base", "develop")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Nothing was committed: ``develop`` is still an unborn branch.
        result = self.run_actl("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("base branch 'develop' has no commits yet", result.stderr)
        self.assertIn("git checkout -b develop", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertFalse((self.repo / ".actl" / "worktrees").exists())

    def test_task_run_works_after_the_initial_commit(self):
        self.make_task()
        self.commit_initial()
        head_before = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.run_actl("task", "run", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        # The run branch is created from the initial commit and the task
        # branch continues from it; the base branch stays put.
        self.assertEqual(
            self.git("rev-parse", "actl/t1~1").stdout.strip(), head_before
        )
        self.assertEqual(
            self.git("rev-parse", "actl/run/r1").stdout.strip(),
            self.git("rev-parse", "actl/t1").stdout.strip(),
        )
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head_before)

        result = self.run_actl("run", "merge", "r1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(
            self.git("rev-parse", "HEAD").stdout.strip(),
            self.git("rev-parse", "actl/t1").stdout.strip(),
        )

    def test_configured_base_must_have_commits_before_running(self):
        self.make_task()
        result = self.run_actl("init", "--base", "develop")
        self.assertEqual(result.returncode, 0, result.stderr)
        # This commits on the unborn HEAD branch, not on ``develop``, so the
        # configured base still has no commit; the run explains how to
        # create it.
        self.commit_initial()
        result = self.run_actl("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch 'develop' does not exist", result.stderr)
        self.assertIn("git branch develop", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertFalse((self.repo / ".actl" / "worktrees").exists())

        self.git("checkout", "-q", "-b", "develop")
        develop_before = self.git("rev-parse", "develop").stdout.strip()
        result = self.run_actl("task", "run", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(
            self.git("rev-parse", "actl/t1~1").stdout.strip(), develop_before
        )
        self.assertEqual(
            self.git("rev-parse", "actl/run/r1").stdout.strip(),
            self.git("rev-parse", "actl/t1").stdout.strip(),
        )
        self.assertEqual(
            self.git("rev-parse", "develop").stdout.strip(), develop_before
        )


class DependencyTests(TaskRunTestCase):
    def test_task_with_dependencies_is_refused(self):
        self.make_task()
        self.assertEqual(
            self.run_actl(
                "task", "create", "--title", "Second", "--depends-on", "t1"
            ).returncode,
            0,
        )
        result = self.run_task("t2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("depends on t1 (PENDING)", result.stderr)
        self.assertIn("DONE or STAGED in the current run", result.stderr)
        self.assertEqual(self.task_status("t2"), "PENDING")
        self.assertFalse(self.worktree("t2").exists())
        self.assertEqual(self.worker_calls(), [])

    def test_dependency_unlocks_after_the_dependency_is_done(self):
        self.make_task()
        self.assertEqual(
            self.run_actl(
                "task", "create", "--title", "Second", "--depends-on", "t1"
            ).returncode,
            0,
        )
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status("t1"), "STAGED")

        # STAGED in another (completed) run is not DONE: the dependent must
        # wait until the run is integrated.
        refused = self.run_task("t2")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Dependency t1 is STAGED in run r1", refused.stderr)
        self.assertIn("actl run merge r1", refused.stderr)
        self.assertEqual(self.task_status("t2"), "PENDING")

        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.task_status("t1"), "DONE")

        result = self.run_task("t2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status("t2"), "STAGED")
        self.assertEqual(self.rev("actl/run/r2"), self.rev("actl/t2"))
        self.assertEqual(self.rev("main"), self.rev("actl/t1"))
        self.git("merge-base", "--is-ancestor", "actl/t1", "actl/t2")


class ExecutionLockTests(TaskRunTestCase):
    def test_second_execution_is_refused_while_the_lock_is_held(self):
        self.make_task()
        lock_path = self.repo / ".actl" / "execution.lock"
        handle = open(lock_path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = self.run_task("t1")
        finally:
            handle.close()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already running", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertEqual(self.worker_calls(), [])
        self.assertFalse(self.worktree().exists())

    def test_lock_is_released_when_execution_finishes(self):
        self.make_task()
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.env["ACTL_FAKE_RUN_EXIT"] = "0"
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lock_survives_parent_termination_while_the_worker_lives(self):
        self.make_task()
        marker = self.base / "worker.pid"
        release = self.base / "worker.release"
        done = self.base / "worker.done"
        self.addCleanup(self.kill_pid, marker)
        self.env["ACTL_FAKE_RUN_MARKER"] = str(marker)
        self.env["ACTL_FAKE_RUN_WAIT_FOR"] = str(release)
        self.env["ACTL_FAKE_RUN_DONE"] = str(done)

        parent = subprocess.Popen(
            [sys.executable, "-m", "actl", "task", "run", "t1"],
            cwd=str(self.repo), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertTrue(self.wait_for_path(marker), "the worker never started")
            # The worker is alive and holds the lock through actl's fd.
            refused = self.run_task("t1")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("already running", refused.stderr)

            parent.kill()
            parent.wait(timeout=30)
            self.assertNotEqual(parent.poll(), None)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=30)

        # actl is dead but its worker still holds the inherited lock.
        refused = self.run_task("t1")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("already running", refused.stderr)
        self.assertEqual(self.task_status(), "RUNNING")

        release.write_text("go", encoding="utf-8")
        self.assertTrue(self.wait_for_path(done), "the worker never finished")
        self.assertTrue(
            self.wait_for_lock_release(), "the worker's lock was never released"
        )

        # An explicit retry continues from the interrupted execution.
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("left RUNNING", result.stdout)
        self.assertIn("Reusing worktree", result.stdout)
        self.assertEqual(self.task_status(), "STAGED")
        # The interrupted run was resumed, not replaced: the same run
        # finished with the task staged.
        self.assertEqual(self.run_status("r1"), "COMPLETE")
        subjects = self.git("log", "--format=%s", "actl/t1").stdout.splitlines()
        self.assertEqual(subjects.count("worker: implement task"), 2)


if __name__ == "__main__":
    unittest.main()
