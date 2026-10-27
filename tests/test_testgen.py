"""End-to-end tests for the test-generation stage.

Before the first worker attempt, ``actl task run`` launches a noninteractive
OpenCode test writer in the task worktree with the project's test-writer
model. The fake test writer either decides that no tests are needed (the
default; it changes nothing) or commits test changes; the stage has its own
timestamped log, records its usage under the distinct ``test_writer`` stage,
and treats execution errors, invalid outcomes, uncommitted changes, and a
dirty worktree as failures instead of starting the worker.
"""

from __future__ import annotations

import json
import shutil
import unittest

from test_verification import VerificationTestCase

TESTS_WRITTEN = {"decision": "tests_written", "summary": "added tests"}


@unittest.skipUnless(shutil.which("git"), "git is required")
class TestGenTestCase(VerificationTestCase):
    """A project with an explicitly configured test-writer model."""

    def setUp(self):
        super().setUp()
        result = self.run_actl("init", "--test-writer-model", "writer/model")
        self.assertEqual(result.returncode, 0, result.stderr)

    # -- helpers ---------------------------------------------------------

    def generation_prompt(self):
        calls = self.generation_calls()
        self.assertTrue(calls, "the test writer was never invoked")
        return calls[-1]["argv"][-1]

    def outcome_record(self, task_id=1):
        path = self.repo / ".actl" / "testgen" / ("t" + str(task_id))
        self.assertTrue(path.is_file(), "no test-generation outcome record")
        return json.loads(path.read_text(encoding="utf-8"))

    def latest_generation_log(self):
        paths = self.generation_logs()
        self.assertTrue(paths, "no test-writer log was written")
        return paths[-1].read_text(encoding="utf-8")

    def call_index(self, call):
        """The position of ``call`` in the fake's invocation log."""
        return self.calls().index(call)


class TestGenerationStageTests(TestGenTestCase):
    def test_test_writer_runs_before_the_worker_with_model_and_task(self):
        self.make_task("Add the login form")
        self.run_actl(
            "task", "update", "t1", "--description",
            "First line.\nSecond line.",
        )
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("test writer model: writer/model", result.stdout)

        calls = self.generation_calls()
        self.assertEqual(len(calls), 1)
        call = calls[0]
        argv = call["argv"]
        self.assertIn("writer/model", argv)
        self.assertEqual(argv[argv.index("--agent") + 1], "build")
        self.assertEqual(argv[argv.index("--format") + 1], "json")
        self.assertIn("--session", argv)
        self.assertEqual(call["cwd"], str(self.worktree()))
        prompt = self.generation_prompt()
        self.assertIn("Task t1: Add the login form", prompt)
        self.assertIn("First line.", prompt)
        self.assertIn("Second line.", prompt)
        self.assertIn("(branch actl/t1)", prompt)
        self.assertIn("Never merge into actl/run/r1.", prompt)

        # The test writer runs before the worker, in the same worktree.
        self.assertLess(
            self.call_index(call), self.call_index(self.worker_calls()[0])
        )
        self.assertIn(
            "Test writer started (model writer/model)", result.stdout
        )
        self.assertIn("tests log: ", result.stdout)
        self.assertIn(
            "Test generation finished (no tests needed)", result.stdout
        )
        # The transcript stays in the log.
        self.assertNotIn("fake opencode:", result.stdout)
        self.assertNotIn("fake opencode:", result.stderr)

    def test_no_tests_needed_succeeds_without_a_commit(self):
        # The fake test writer changes nothing by default and decides that
        # no tests are needed: the stage succeeds without a commit and the
        # worker's commit is the only change on the branch.
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.rev("actl/t1~1"), self.base_sha)
        subjects = self.git(
            "log", "--format=%s", "actl/t1"
        ).stdout.splitlines()
        self.assertEqual(subjects[0], "worker: implement task")
        self.assertEqual(
            self.git("status", "--porcelain", cwd=self.worktree()).stdout, ""
        )
        self.assertEqual(
            self.outcome_record(), {"decision": "no_tests_needed"}
        )
        log = self.latest_generation_log()
        self.assertTrue(log.startswith("actl test generation\n"), log[:80])
        self.assertIn("model: writer/model\n", log)
        self.assertIn("branch: actl/t1\n", log)
        self.assertIn("exit_code: 0\n", log)
        self.assertIn("result: no_tests_needed\n", log)

    def test_committed_test_changes_succeed(self):
        self.env["ACTL_FAKE_TESTS"] = "commit"
        self.env["ACTL_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn(
            "Test generation finished (tests committed)", result.stdout
        )
        # The worker started from the committed tests.
        subjects = self.git(
            "log", "--format=%s", "actl/t1"
        ).stdout.splitlines()
        self.assertEqual(subjects[0], "worker: implement task")
        self.assertEqual(subjects[1], "test writer: add tests")
        self.assertTrue((self.worktree() / "test-output.txt").is_file())
        self.assertEqual(
            self.git("status", "--porcelain", cwd=self.worktree()).stdout, ""
        )
        self.assertEqual(
            self.outcome_record(), {"decision": "tests_written"}
        )
        self.assertIn("result: tests_written\n", self.latest_generation_log())

    def test_tests_written_without_a_commit_fails_the_stage(self):
        # The default test writer changes nothing; claiming tests_written
        # anyway is an invalid outcome, and the worker must not start.
        self.env["ACTL_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("test generation: ", result.stderr)
        self.assertIn("committed no test changes", result.stderr)
        self.assertIn("-tests.log)", result.stderr)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")
        self.assertEqual(self.reviewer_calls(), [])
        self.assertIn("result: FAILED\n", self.latest_generation_log())
        self.assertFalse(
            (self.repo / ".actl" / "testgen" / "t1").exists(),
            "a failed stage must not be recorded as completed",
        )

    def test_uncommitted_changes_fail_the_stage(self):
        self.env["ACTL_FAKE_TESTS"] = "dirty"
        self.env["ACTL_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("left the worktree dirty", result.stderr)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")
        self.assertIn("result: FAILED\n", self.latest_generation_log())

    def test_untracked_files_fail_the_stage(self):
        self.env["ACTL_FAKE_TESTS"] = "untracked"
        self.env["ACTL_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("left the worktree dirty", result.stderr)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")

    def test_test_writer_execution_error_fails_the_stage(self):
        self.env["ACTL_FAKE_TESTS_EXIT"] = "3"
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("exited with status 3", result.stderr)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")
        self.assertEqual(self.reviewer_calls(), [])
        log = self.latest_generation_log()
        self.assertIn("exit_code: 3\n", log)
        self.assertIn("result: FAILED\n", log)

    def test_invalid_outcomes_fail_the_stage(self):
        self.make_task()
        for message, expected in (
            ("prose without any JSON", "not one complete JSON object"),
            ('{"decision": "maybe"}', "unknown test-writer decision"),
            ("[1, 2, 3]", "not a JSON object"),
        ):
            with self.subTest(message=message):
                self.env["ACTL_FAKE_TESTS_JSON"] = message
                result = self.run_task("t1")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.task_status(), "FAILED")
                self.assertIn(expected, result.stderr)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")

    def test_a_fenced_decision_is_accepted(self):
        self.env["ACTL_FAKE_TESTS_JSON"] = (
            '```json\n{"decision":"no_tests_needed","reason":"none"}\n```'
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")

    def test_empty_commit_with_tests_written_fails_the_stage(self):
        # An empty commit is not a test change: the branch still has no
        # committed changes relative to the run branch.
        self.env["ACTL_FAKE_TESTS"] = "empty"
        self.env["ACTL_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("committed no test changes", result.stderr)

    def test_completed_stage_is_not_repeated_on_retry(self):
        # The worker fails after the test-generation stage completed; the
        # recorded outcome keeps the retry from running the stage again.
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(len(self.generation_calls()), 1)

        self.env["ACTL_FAKE_RUN_EXIT"] = "0"
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(
            len(self.generation_calls()), 1, "the stage must not repeat"
        )
        self.assertEqual(len(self.worker_calls()), 2)

    def test_failed_stage_reruns_on_retry(self):
        # A failed stage is never recorded as completed: the retry runs it
        # again before the worker.
        self.env["ACTL_FAKE_TESTS_EXIT"] = "1"
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(len(self.generation_calls()), 1)
        self.assertEqual(self.worker_calls(), [])

        self.env["ACTL_FAKE_TESTS_EXIT"] = "0"
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.generation_calls()), 2)
        self.assertEqual(len(self.worker_calls()), 1)

    def test_committed_work_skips_the_stage_without_a_record(self):
        # The outcome record can be lost (a crash between the test writer's
        # commit and the record, or an implementation left by an older actl):
        # a branch that already carries committed changes never gets a fresh
        # test-writing turn.
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(len(self.generation_calls()), 1)
        (self.repo / ".actl" / "testgen" / "t1").unlink()

        del self.env["ACTL_FAKE_RUN_EXIT"]
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(
            len(self.generation_calls()), 1, "no fresh test-writing turn"
        )
        self.assertEqual(len(self.worker_calls()), 2)

    def test_each_task_of_a_batch_gets_its_own_stage(self):
        self.make_task("First")
        created = self.run_actl("task", "create", "--title", "Second")
        self.assertEqual(created.returncode, 0, created.stderr)

        result = self.run_task("t1", "t2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call["cwd"] for call in self.generation_calls()],
            [str(self.worktree("t1")), str(self.worktree("t2"))],
        )
        self.assertEqual(
            [self.outcome_record(1), self.outcome_record(2)],
            [{"decision": "no_tests_needed"}, {"decision": "no_tests_needed"}],
        )


if __name__ == "__main__":
    unittest.main()
