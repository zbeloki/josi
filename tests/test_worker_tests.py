"""Regressions for worker-owned tests and removal of test generation.

The backend fakes commit runnable unittest coverage with the implementation.
Historical state is seeded directly: it must remain usable even when the old
stage and its outcome parser no longer exist.
"""
from __future__ import annotations

import json
import shlex
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_verification import VerificationTestCase
from test_repair import RepairTestCase
from test_claude import ClaudeTestCase
from test_codex import CodexTestCase

from josi import agents, execution, review, tasks


class WorkerTestPromptTests(unittest.TestCase):
    def setUp(self):
        self.task = SimpleNamespace(id=1, title="Add answer", description="Return 42.")
        self.worktree = execution.Worktree("josi/t1", Path("/tmp/task"), True)

    def test_each_backend_has_only_three_execution_model_roles(self):
        expected = {"worker_model", "reviewer_model", "escalation_worker_model"}
        self.assertEqual(set(agents.MODEL_KEYS), expected)
        for backend in ("opencode", "claude", "codex"):
            with self.subTest(backend=backend):
                self.assertEqual(set(agents.DEFAULT_MODELS[backend]), expected)

    def test_worker_owns_tests_and_preserves_useful_coverage(self):
        for attempt in (1, 2, 3):
            with self.subTest(attempt=attempt):
                prompt = execution.worker_prompt(
                    self.task, self.worktree, "josi/run/r1", attempt, 3,
                    "Review: add an assertion for invalid input in test_feature.py.",
                )
                text = " ".join(prompt.lower().split())
                self.assertNotIn("do not write, add, modify", text)
                self.assertNotIn("separate test-generation", text)
                self.assertNotIn("keep any existing tests as they are", text)
                self.assertRegex(text, r"(?:add|write).{0,100}tests")
                self.assertRegex(text, r"(?:update|modify).{0,100}tests|tests.{0,100}(?:update|modify)")
                self.assertRegex(text, r"run.{0,100}(?:tests|checks).{0,100}verif")
                self.assertRegex(text, r"(?:address|fix|resolve).{0,150}(?:validation|review)|(?:validation|review).{0,150}(?:address|fix|resolve)")
                self.assertRegex(text, r"(?:preserv|retain|keep).{0,100}(?:coverage|useful tests)")
                self.assertRegex(text, r"(?:weaken|delet).{0,150}(?:hide|conceal|mask)")
                if attempt > 1:
                    self.assertIn("test_feature.py", prompt)
                    self.assertIn("add an assertion for invalid input", prompt)
                    self.assertIn("Do not reset the branch", prompt)

    def test_readme_removes_obsolete_cli_and_configuration_examples(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertNotIn("--test-writer-model", readme)
        self.assertNotIn('"test_writer_model"', readme)
        text = " ".join(readme.lower().split())
        self.assertRegex(text, r"worker.{0,200}(?:add|write|own|update).{0,100}tests")

    def test_reviewer_assesses_tests_even_with_project_policy(self):
        for policy in (review.BUILTIN_POLICY, "Approve only correct implementations."):
            with self.subTest(policy=policy):
                prompt = review.build_prompt(
                    self.task, "base", "head", "diff", "Validation passed", policy,
                )
                text = " ".join(prompt.lower().split())
                self.assertRegex(text, r"(?:test|coverage).{0,100}(?:adequ|meaningful)|meaningful.{0,100}(?:test|coverage)")
                self.assertRegex(text, r"(?:unjustif|unnecess|without justification).{0,150}(?:weaken|reduc|delet)|(?:weaken|reduc|delet).{0,150}(?:unjustif|without justification)")


class WorkerOwnedExecutionTests(RepairTestCase):
    def setUp(self):
        super().setUp()
        self.env["JOSI_FAKE_RUN"] = "commit-tests"
        self.set_validation(shlex.quote(sys.executable) + " -B -m unittest discover -v")

    def assert_no_test_writer_artifacts(self, result):
        self.assertEqual(self.generation_calls(), [])
        self.assertEqual(self.generation_logs(), [])
        self.assertFalse((self.repo / ".josi/testgen").exists())
        for text in (result.stdout, result.stderr):
            self.assertNotIn("Test writer", text)
            self.assertNotIn("Test generation", text)
            self.assertNotIn("tests log:", text)

    def test_worker_commit_with_tests_is_validated_then_independently_reviewed(self):
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_test_writer_artifacts(result)
        self.assertEqual(self.task_status(), "STAGED")
        turns = [c for c in self.calls() if c["argv"][:1] == ["run"]]
        self.assertEqual([self.agent_of(c) for c in turns], ["build", "plan"])
        self.assertIn("Ran 1 test", self.validation_log())
        self.assertIn("test_answer", self.validation_log())
        self.assertIn("All configured validation commands passed", self.reviewer_prompt())
        self.assertIn("feature.py", self.reviewer_prompt())
        self.assertIn("test_feature.py", self.reviewer_prompt())
        self.assertNotEqual(self.reviewer_calls()[0]["cwd"], str(self.worktree()))
        self.assertEqual(self.rev("josi/t1~1"), self.base_sha)
        self.assertEqual(
            set(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "josi/t1").stdout.splitlines()),
            {"feature.py", "test_feature.py"},
        )
        self.assertEqual(self.attempt_record()["attempts"], 1)

    def test_failed_assertion_reaches_worker_and_worker_can_fix_it(self):
        self.set_worker("failing-tests", "commit-tests")
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.attempt_record()["attempts"], 2)
        self.assertEqual(len(self.reviewer_calls()), 1)
        prompt = self.worker_calls()[1]["argv"][-1]
        self.assertIn("test_answer", prompt)
        self.assertIn("AssertionError", prompt)
        self.assertIn("42 != 41", prompt)
        self.assertIn("self.assertEqual(answer(), 42)", self.reviewer_prompt())
        self.assert_no_test_writer_artifacts(result)

    def test_review_requested_test_addition_is_committed_and_revalidated(self):
        self.set_worker("commit-tests", "extend-tests")
        self.set_reviewer(
            self.major("Add return-type coverage.", location="test_feature.py"),
            self.approved(),
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Add return-type coverage.", self.worker_calls()[1]["argv"][-1])
        self.assertIn("test_answer_type", self.reviewer_prompt())
        self.assertIn("Ran 2 tests", self.validation_log())
        self.assertEqual(self.rev("josi/t1~2"), self.base_sha)
        self.assertEqual(self.attempt_record()["attempts"], 2)
        self.assert_no_test_writer_artifacts(result)

    def test_review_test_feedback_reaches_escalation_without_resetting_commits(self):
        self.set_worker("commit-tests", "failing-tests", "commit-tests")
        self.set_reviewer(
            self.major("Add invalid-input coverage.", location="test_feature.py"),
            self.approved(),
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.worker_models(), ["provider/model", "provider/model", "escalation/model"])
        self.assertEqual(self.attempt_record()["attempts"], 3)
        self.assertIn("Add invalid-input coverage.", self.worker_calls()[1]["argv"][-1])
        self.assertIn("test_feature.py", self.worker_calls()[1]["argv"][-1])
        self.assertIn("AssertionError", self.worker_calls()[2]["argv"][-1])
        self.assertEqual(self.rev("josi/t1~3"), self.base_sha)
        self.assertEqual(len(self.reviewer_calls()), 2)
        self.assert_no_test_writer_artifacts(result)


class HistoricalTestGenerationResumeTests(VerificationTestCase):
    def seed_interrupted_run(self, status="RUNNING", attempts=0, prior_commit=False):
        self.make_task()
        josi_dir = self.repo / ".josi"
        conn = tasks.connect(josi_dir / "tasks.db")
        try:
            run = tasks.create_run(conn, "main", self.base_sha, [1])
            task = tasks.get_task(conn, 1)
            tasks.set_task_status(conn, 1, status)
        finally:
            conn.close()
        execution.ensure_run_worktree(self.repo, josi_dir, run)
        execution.ensure_worktree(self.repo, josi_dir, task, True, run.branch)
        execution.record_attempt_state(josi_dir, 1, attempts, "Add missing assertions in test_feature.py.")
        if prior_commit:
            (self.worktree() / "test_legacy.py").write_text("# historical test writer coverage\n")
            self.commit("test writer: add tests", cwd=self.worktree())
        return self.rev("josi/t1")

    def test_legacy_markers_are_ignored_byte_for_byte_on_resume(self):
        self.seed_interrupted_run(attempts=1, prior_commit=True)
        marker = self.repo / ".josi/testgen/t1"
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b'{"decision":"tests_written"}\n')
        old_commit = self.rev("josi/t1")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Worker attempt 2/3", result.stdout)
        self.assertIn("test_legacy.py", self.reviewer_prompt())
        self.assertEqual(self.rev("josi/t1~1"), old_commit)
        self.assertEqual(marker.read_bytes(), b'{"decision":"tests_written"}\n')
        self.assertEqual(self.generation_calls(), [])
        self.assertEqual(self.generation_logs(), [])

    def test_malformed_marker_does_not_restart_generation(self):
        self.seed_interrupted_run()
        marker = self.repo / ".josi/testgen/t1"
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b'not JSON\xff')
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_bytes(), b'not JSON\xff')
        self.assertEqual(self.generation_calls(), [])
        self.assertIn("Worker attempt 1/3", result.stdout)

    def test_failed_generation_without_a_commit_starts_first_worker_attempt(self):
        self.seed_interrupted_run("FAILED")
        historical_log = self.repo / ".josi/runs/t1-failed-tests.log"
        historical_log.parent.mkdir(parents=True, exist_ok=True)
        historical_log.write_text("josi test generation\nexit_code: 3\nresult: FAILED\n")
        original = historical_log.read_bytes()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Worker attempt 1/3", result.stdout)
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertEqual(self.generation_calls(), [])
        self.assertEqual(self.generation_logs(), [historical_log])
        self.assertEqual(historical_log.read_bytes(), original)
        self.assertFalse((self.repo / ".josi/testgen").exists())

    def test_completed_marker_without_a_commit_is_preserved(self):
        self.seed_interrupted_run()
        marker = self.repo / ".josi/testgen/t1"
        marker.parent.mkdir(parents=True)
        marker.write_text('{"decision":"no_tests_needed"}\n')
        original = marker.read_bytes()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual(self.generation_calls(), [])
        self.assertEqual(self.generation_logs(), [])
        self.assertIn("Worker attempt 1/3", result.stdout)

    def test_previously_failed_generation_skips_stage_and_preserves_history(self):
        old_commit = self.seed_interrupted_run("FAILED", prior_commit=True)
        historical_log = self.repo / ".josi/runs/t1-historical-tests.log"
        historical_log.parent.mkdir(parents=True, exist_ok=True)
        historical_log.write_text("josi test generation\nresult: FAILED\n")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Worker attempt 1/3", result.stdout)
        self.assertEqual(self.rev("josi/t1~1"), old_commit)
        self.assertEqual(historical_log.read_text(), "josi test generation\nresult: FAILED\n")
        self.assertEqual(self.generation_calls(), [])
        self.assertEqual(self.generation_logs(), [historical_log])
        self.assertFalse((self.repo / ".josi/testgen").exists())

    def test_prior_attempts_remain_consumed_after_failed_generation(self):
        old_commit = self.seed_interrupted_run("FAILED", attempts=2, prior_commit=True)
        self.set_validation("exit 3")
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertIn("Worker attempt 3/3 (escalation/model)", result.stdout)
        self.assertIn("Add missing assertions", self.worker_prompt())
        self.assertEqual(self.rev("josi/t1~1"), old_commit)
        self.assertEqual(self.generation_calls(), [])
        again = self.run_task("t1")
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(len(self.worker_calls()), 1)


class BackendWorkerTests:
    """Shared CLI contract for the Claude and Codex subprocess protocols."""
    backend = None

    def setUp(self):
        super().setUp()
        self.commit_base()
        result = self.run_josi("init", "--agent", self.backend)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_tests_are_validated_before_review_without_extra_sessions(self):
        metadata = self.metadata()
        checked = self.base / "validation-head"
        command = (
            shlex.quote(sys.executable) + " -B -m unittest discover -v && git rev-parse HEAD > "
            + shlex.quote(str(checked))
        )
        metadata["validation"] = {"commands": [command]}
        self.metadata_path().write_text(json.dumps(metadata))
        self.assertEqual(self.run_josi("task", "create", "--title", "Feature").returncode, 0)
        result = self.run_josi("task", "run", "t1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        worker, reviewer = self.calls()
        self.assertTrue(worker["stdin"].startswith("Implement this task"))
        self.assertIn("independent code reviewer", reviewer["stdin"])
        self.assertIn(checked.read_text().strip(), reviewer["stdin"])
        self.assertIn("test_feature.py", reviewer["stdin"])
        self.assertIn("All configured validation commands passed", reviewer["stdin"])
        self.assertNotEqual(worker["cwd"], reviewer["cwd"])
        self.assertFalse((self.repo / ".josi/testgen").exists())
        self.assertEqual(list((self.repo / ".josi/runs").glob("*-tests.log")), [])
        conn = tasks.connect(self.repo / ".josi/tasks.db")
        try:
            self.assertEqual([r.stage for r in tasks.task_usage(conn, 1)], ["worker", "review"])
        finally:
            conn.close()

    def test_obsolete_model_is_ignored_by_init_plan_and_execution(self):
        metadata = self.metadata()
        metadata["test_writer_model"] = {"model": [], "effort": "not an effort"}
        self.metadata_path().write_text(json.dumps(metadata))
        for args in (("init",), ("plan",), ("task", "create", "--title", "Feature"),
                     ("task", "run", "t1")):
            result = self.run_josi(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["test_writer_model"], metadata["test_writer_model"])
        self.assertFalse(any(c["stdin"].startswith("Write the tests") for c in self.calls()))


class ClaudeWorkerOwnedTests(BackendWorkerTests, ClaudeTestCase):
    backend = "claude"


class CodexWorkerOwnedTests(BackendWorkerTests, CodexTestCase):
    backend = "codex"
