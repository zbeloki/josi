"""End-to-end tests for the test-generation stage.

Before the first worker attempt, ``josi task run`` launches a noninteractive
OpenCode test writer in the task worktree with the project's test-writer
model. The fake test writer either decides that no tests are needed (the
default; it changes nothing) or commits test changes; the stage has its own
timestamped log, records its usage under the distinct ``test_writer`` stage,
and treats execution errors, invalid outcomes, uncommitted changes, and a
dirty worktree as failures instead of starting the worker. A final message
that is not one JSON object is reformatted once by the same session before
the stage fails.

These tests also cover the stage's integration into task execution: a
completed stage is never repeated - not for a repair attempt and not when an
interrupted execution is resumed - a failed stage consumes no worker attempt,
the worker prompt tells the worker to keep the generated tests unchanged, the
test-writer commit stays part of the reviewed changes, and tasks that need no
worker (approved or verification-only) never launch the stage. The stage
never runs the generated tests itself: the project's configured validation
commands exercise them after the worker, and validation stays optional when
no commands are configured.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest

from test_verification import VerificationTestCase

from josi import tasks

TESTS_WRITTEN = {"decision": "tests_written", "summary": "added tests"}


@unittest.skipUnless(shutil.which("git"), "git is required")
class TestGenTestCase(VerificationTestCase):
    """A project with an explicitly configured test-writer model."""

    def setUp(self):
        super().setUp()
        result = self.run_josi("init", "--test-writer-model", "writer/model")
        self.assertEqual(result.returncode, 0, result.stderr)

    # -- helpers ---------------------------------------------------------

    def generation_prompt(self):
        calls = self.generation_calls()
        self.assertTrue(calls, "the test writer was never invoked")
        return calls[-1]["argv"][-1]

    def outcome_record(self, task_id=1):
        path = self.repo / ".josi" / "testgen" / ("t" + str(task_id))
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
        self.run_josi(
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
        self.assertIn("(branch josi/t1)", prompt)
        self.assertIn("Never merge into josi/run/r1.", prompt)

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
        self.assertEqual(self.rev("josi/t1~1"), self.base_sha)
        subjects = self.git(
            "log", "--format=%s", "josi/t1"
        ).stdout.splitlines()
        self.assertEqual(subjects[0], "worker: implement task")
        self.assertEqual(
            self.git("status", "--porcelain", cwd=self.worktree()).stdout, ""
        )
        self.assertEqual(
            self.outcome_record(), {"decision": "no_tests_needed"}
        )
        log = self.latest_generation_log()
        self.assertTrue(log.startswith("josi test generation\n"), log[:80])
        self.assertIn("model: writer/model\n", log)
        self.assertIn("branch: josi/t1\n", log)
        self.assertIn("exit_code: 0\n", log)
        self.assertIn("result: no_tests_needed\n", log)

    def test_committed_test_changes_succeed(self):
        self.env["JOSI_FAKE_TESTS"] = "commit"
        self.env["JOSI_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn(
            "Test generation finished (tests committed)", result.stdout
        )
        # The worker started from the committed tests.
        subjects = self.git(
            "log", "--format=%s", "josi/t1"
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
        self.env["JOSI_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
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
            (self.repo / ".josi" / "testgen" / "t1").exists(),
            "a failed stage must not be recorded as completed",
        )

    def test_uncommitted_changes_fail_the_stage(self):
        self.env["JOSI_FAKE_TESTS"] = "dirty"
        self.env["JOSI_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("left the worktree dirty", result.stderr)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")
        self.assertIn("result: FAILED\n", self.latest_generation_log())

    def test_untracked_files_fail_the_stage(self):
        self.env["JOSI_FAKE_TESTS"] = "untracked"
        self.env["JOSI_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("left the worktree dirty", result.stderr)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")

    def test_test_writer_execution_error_fails_the_stage(self):
        self.env["JOSI_FAKE_TESTS_EXIT"] = "3"
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
                self.env["JOSI_FAKE_TESTS_JSON"] = message
                result = self.run_task("t1")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.task_status(), "FAILED")
                self.assertIn(expected, result.stderr)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")

    def test_a_fenced_decision_is_accepted(self):
        self.env["JOSI_FAKE_TESTS_JSON"] = (
            '```json\n{"decision":"no_tests_needed","reason":"none"}\n```'
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")

    def test_empty_commit_with_tests_written_fails_the_stage(self):
        # An empty commit is not a test change: the branch still has no
        # committed changes relative to the run branch.
        self.env["JOSI_FAKE_TESTS"] = "empty"
        self.env["JOSI_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("committed no test changes", result.stderr)

    def test_completed_stage_is_not_repeated_on_retry(self):
        # The worker fails after the test-generation stage completed; the
        # recorded outcome keeps the retry from running the stage again.
        self.env["JOSI_FAKE_RUN_EXIT"] = "1"
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(len(self.generation_calls()), 1)

        self.env["JOSI_FAKE_RUN_EXIT"] = "0"
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
        self.env["JOSI_FAKE_TESTS_EXIT"] = "1"
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(len(self.generation_calls()), 1)
        self.assertEqual(self.worker_calls(), [])

        self.env["JOSI_FAKE_TESTS_EXIT"] = "0"
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.generation_calls()), 2)
        self.assertEqual(len(self.worker_calls()), 1)

    def test_committed_work_skips_the_stage_without_a_record(self):
        # The outcome record can be lost (a crash between the test writer's
        # commit and the record, or an implementation left by an older josi):
        # a branch that already carries committed changes never gets a fresh
        # test-writing turn.
        self.env["JOSI_FAKE_RUN_EXIT"] = "1"
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(len(self.generation_calls()), 1)
        (self.repo / ".josi" / "testgen" / "t1").unlink()

        del self.env["JOSI_FAKE_RUN_EXIT"]
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(
            len(self.generation_calls()), 1, "no fresh test-writing turn"
        )
        self.assertEqual(len(self.worker_calls()), 2)

    def test_each_task_of_a_batch_gets_its_own_stage(self):
        self.make_task("First")
        created = self.run_josi("task", "create", "--title", "Second")
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

    def test_repair_attempts_do_not_rerun_the_stage(self):
        # A failing validation command drives the task through all three
        # worker attempts in the same worktree; the completed stage is not
        # repeated for any repair attempt.
        self.set_validation("exit 3")
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertEqual(len(self.worker_calls()), 3)
        self.assertEqual(
            len(self.generation_calls()), 1, "the stage must not repeat"
        )
        self.assertEqual(
            self.outcome_record(), {"decision": "no_tests_needed"}
        )

    def test_resuming_a_completed_stage_does_not_rerun_it(self):
        # The execution is interrupted after the stage completed but before
        # the first worker attempt finished: resuming the task starts the
        # worker without repeating the completed stage.
        self.make_task()
        marker = self.base / "worker.pid"
        release = self.base / "worker.release"
        self.addCleanup(self.kill_pid, marker)
        self.env["JOSI_FAKE_RUN_MARKER"] = str(marker)
        self.env["JOSI_FAKE_RUN_WAIT_FOR"] = str(release)

        parent = subprocess.Popen(
            [sys.executable, "-m", "josi", "task", "run", "t1"],
            cwd=str(self.repo), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertTrue(
                self.wait_for_path(marker), "the worker never started"
            )
        finally:
            parent.kill()
            parent.wait(timeout=30)
            parent.stdout.close()
            parent.stderr.close()
        # The waiting worker holds the inherited lock; kill it too.
        self.kill_pid(marker)
        self.assertTrue(
            self.wait_for_lock_release(),
            "the interrupted worker kept the lock",
        )

        # The stage had completed before the worker was interrupted.
        self.assertEqual(
            self.outcome_record(), {"decision": "no_tests_needed"}
        )
        self.assertEqual(len(self.generation_calls()), 1)
        self.assertEqual(self.task_status(), "RUNNING")
        workers = len(self.worker_calls())

        del self.env["JOSI_FAKE_RUN_MARKER"]
        del self.env["JOSI_FAKE_RUN_WAIT_FOR"]
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("left RUNNING", result.stdout)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(
            len(self.generation_calls()), 1, "the stage must not repeat"
        )
        self.assertEqual(len(self.worker_calls()), workers + 1)

    def test_failed_stage_leaves_the_attempt_budget_unused(self):
        # A failed stage stops before the worker: no worker attempt is
        # consumed, and the retry starts the worker at attempt 1 of 3.
        self.env["JOSI_FAKE_TESTS_EXIT"] = "3"
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(self.worker_calls(), [])
        self.assertFalse(
            (self.repo / ".josi" / "attempts" / "t1").exists(),
            "a failed test-generation stage must not consume an attempt",
        )

        del self.env["JOSI_FAKE_TESTS_EXIT"]
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Worker attempt 1/3", result.stdout)
        self.assertEqual(len(self.worker_calls()), 1)
        record = json.loads(
            (self.repo / ".josi" / "attempts" / "t1").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(record["attempts"], 1)

    def test_worker_prompt_keeps_the_generated_tests(self):
        # The worker is explicitly instructed not to modify the tests the
        # stage generated: it implements the task around them.
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.worker_prompt()
        self.assertIn(
            "Do not write, add, modify, weaken, or delete tests.", prompt
        )
        self.assertIn("keep any\n  existing tests as they are", prompt)

    def test_generated_tests_stay_part_of_the_reviewed_changes(self):
        # The test-writer commit stays on the task branch: validation can
        # run the generated tests in the task worktree, and the reviewer
        # evaluates the cumulative diff that includes them.
        self.env["JOSI_FAKE_TESTS"] = "commit"
        self.env["JOSI_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.set_validation("cat test-output.txt")
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Validation passed", result.stdout)

        subjects = self.git(
            "log", "--format=%s", "josi/t1"
        ).stdout.splitlines()
        self.assertEqual(subjects[0], "worker: implement task")
        self.assertEqual(subjects[1], "test writer: add tests")

        prompt = self.reviewer_prompt()
        self.assertIn("test-output.txt", prompt)
        self.assertIn("test change", prompt)
        self.assertIn(self.base_sha, prompt)

    def test_project_validation_commands_exercise_the_generated_tests(self):
        # The stage never runs the generated tests itself: after the worker,
        # the project's own validation commands run in the task worktree,
        # where they see the test writer's committed tests and the worker's
        # implementation.
        self.env["JOSI_FAKE_TESTS"] = "commit"
        self.env["JOSI_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.set_validation("cat worker-output.txt test-output.txt")
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Validation passed", result.stdout)

        # The command only succeeds because both files exist: the tests were
        # committed before the worker ran, and validation ran after it.
        log = self.validation_log()
        self.assertIn("$ cat worker-output.txt test-output.txt", log)
        self.assertIn("result: PASSED", log)
        # The validated commit is the worker's commit on top of the
        # test-writer commit, so the command ran on the generated tests.
        self.assertIn("commit: " + self.rev("josi/t1"), log)
        subjects = self.git(
            "log", "--format=%s", "josi/t1"
        ).stdout.splitlines()
        self.assertEqual(subjects[0], "worker: implement task")
        self.assertEqual(subjects[1], "test writer: add tests")

    def test_validation_stays_optional_for_generated_tests(self):
        # Without configured validation commands nothing runs the generated
        # tests, and the run still succeeds: validation is the project's
        # choice, never something josi adds.
        self.env["JOSI_FAKE_TESTS"] = "commit"
        self.env["JOSI_FAKE_TESTS_JSON"] = json.dumps(TESTS_WRITTEN)
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn(
            "Validation skipped (no validation commands configured)",
            result.stdout,
        )
        self.assertEqual(self.validation_logs(), [])
        # The generated tests are still committed on the task branch.
        subjects = self.git(
            "log", "--format=%s", "josi/t1"
        ).stdout.splitlines()
        self.assertEqual(subjects[1], "test writer: add tests")

    def test_test_writer_usage_is_recorded_in_its_own_log(self):
        # The test-generation stage's usage goes into its own -tests.log and
        # is stored under the distinct test_writer stage, separate from the
        # worker's and reviewer's records.
        self.env["JOSI_FAKE_SESSION_USAGE"] = json.dumps({
            "cost": 0.004,
            "tokens": {"input": 400, "output": 40,
                       "cache": {"read": 0, "write": 0}},
        })
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("usage: input=", result.stdout)

        logs = self.generation_logs()
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0].name.endswith("-tests.log"))
        self.assertTrue(self.log_paths(), "the worker keeps its own log")
        log = logs[0].read_text(encoding="utf-8")
        self.assertIn("usage: input=400 output=40", log)
        self.assertIn("cost: total=0.004000", log)
        self.assertIn("usage_status: available", log)

        conn = tasks.connect(self.repo / ".josi" / "tasks.db")
        try:
            rows = tasks.task_usage(conn, 1)
        finally:
            conn.close()
        test_writer = [row for row in rows if row.stage == "test_writer"]
        self.assertEqual(len(test_writer), 1)
        self.assertEqual(test_writer[0].attempt, 1)
        self.assertEqual(test_writer[0].model, "writer/model")
        self.assertEqual(test_writer[0].input_tokens, 400)
        self.assertEqual(test_writer[0].output_tokens, 40)
        self.assertAlmostEqual(test_writer[0].cost_total, 0.004)

    def test_approved_task_does_not_launch_generation(self):
        # An APPROVED task needs no worker: the retry stages the recorded
        # commit without any agent turn, the test writer included.
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        generations = len(self.generation_calls())
        workers = len(self.worker_calls())
        reviewers = len(self.reviewer_calls())

        # An interruption between the approval and the staging status.
        self.set_task_status("APPROVED")
        self.interrupt_run()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(
            len(self.generation_calls()), generations,
            "an approved task must not launch test generation",
        )
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviewers)

    def test_verification_only_resume_does_not_launch_generation(self):
        # An IMPLEMENTED task is re-validated and re-reviewed without a
        # worker; no test-generation turn launches for it either.
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        generations = len(self.generation_calls())
        workers = len(self.worker_calls())

        # An interrupted execution left the task at IMPLEMENTED, and a
        # human fix was committed on its branch before the resume.
        self.set_task_status("IMPLEMENTED")
        self.interrupt_run()
        worktree = self.worktree()
        (worktree / "second.txt").write_text(
            "second change\n", encoding="utf-8"
        )
        self.commit("second change", cwd=worktree)

        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(
            len(self.generation_calls()), generations,
            "a verification-only resume must not launch test generation",
        )
        self.assertEqual(len(self.worker_calls()), workers)


class TestGenerationFormatRetryTests(TestGenTestCase):
    """A malformed final message is reformatted once, like a verdict."""

    def set_tests_sequence(self, entries, name="tests-count"):
        self.env["JOSI_FAKE_TESTS_SEQUENCE"] = json.dumps(entries)
        self.env["JOSI_FAKE_TESTS_COUNT_FILE"] = str(self.base / name)

    def test_prose_before_the_decision_is_reformatted(self):
        # The test writer commits tests but wraps its decision in prose: the
        # stage asks the same session once more for JSON only, keeps the
        # commit, and the worker starts from it.
        self.set_tests_sequence([
            {"behavior": "commit",
             "message": "All tests pass.\n\n" + json.dumps(TESTS_WRITTEN)},
            {"message": json.dumps(TESTS_WRITTEN)},
        ])
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn(
            "Test writer output format invalid; retrying once", result.stdout
        )

        calls = self.generation_calls()
        self.assertEqual(len(calls), 2)
        first, retry = calls
        self.assertEqual(
            first["argv"][first["argv"].index("--session") + 1],
            retry["argv"][retry["argv"].index("--session") + 1],
            "the retry must reuse the test-writer session",
        )
        self.assertEqual(retry["cwd"], first["cwd"])
        self.assertEqual(
            retry["argv"][retry["argv"].index("--agent") + 1], "build"
        )
        self.assertIn("exactly one JSON object", retry["argv"][-1])
        self.assertIn("Do not use tools", retry["argv"][-1])

        log = self.latest_generation_log()
        self.assertIn("format_error: ", log)
        self.assertIn("format_retry: 1 of 1", log)
        self.assertIn("result: tests_written\n", log)
        self.assertEqual(self.outcome_record(), {"decision": "tests_written"})

        # The committed tests survive the reformatting turn, and the worker
        # implements the task on top of them.
        subjects = self.git(
            "log", "--format=%s", "josi/t1"
        ).stdout.splitlines()
        self.assertEqual(subjects[0], "worker: implement task")
        self.assertEqual(subjects[1], "test writer: add tests")
        self.assertEqual(len(self.worker_calls()), 1)

    def test_format_retry_is_bounded(self):
        self.set_tests_sequence([
            {"message": "prose\n\n" + json.dumps(TESTS_WRITTEN)},
            {"message": "still prose, no JSON at all"},
        ])
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("not one complete JSON object", result.stderr)
        self.assertEqual(len(self.generation_calls()), 2)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")
        log = self.latest_generation_log()
        self.assertIn("format_retry: 1 of 1", log)
        self.assertIn("result: FAILED\n", log)

    def test_unusable_decision_is_not_reformatted(self):
        # An unknown decision is unusable, not badly formatted: it fails the
        # stage without spending a reformatting turn.
        self.env["JOSI_FAKE_TESTS_JSON"] = '{"decision": "maybe"}'
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("unknown test-writer decision", result.stderr)
        self.assertEqual(len(self.generation_calls()), 1)
        self.assertEqual(self.worker_calls(), [])

    def test_retry_that_changes_the_branch_fails_the_stage(self):
        # A reformatting turn may only restate the decision: a turn that
        # commits again moves the branch and fails the stage.
        self.set_tests_sequence([
            {"behavior": "commit",
             "message": "prose\n\n" + json.dumps(TESTS_WRITTEN)},
            {"behavior": "commit", "message": json.dumps(TESTS_WRITTEN)},
        ])
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("changed the branch while reformatting", result.stderr)
        self.assertEqual(len(self.generation_calls()), 2)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")


if __name__ == "__main__":
    unittest.main()
