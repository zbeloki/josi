"""End-to-end tests for actl Stage 6: automatic repair and model escalation.

Every test runs the CLI against a disposable Git repository and the fake
``opencode`` executable. The fake supports per-invocation sequences
(``ACTL_FAKE_REVIEW_SEQUENCE`` and ``ACTL_FAKE_RUN_SEQUENCE`` with a count
file), so the suite can reject the first attempt, accept the second, and
inspect the feedback the worker received - no real OpenCode process and no
model call.
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

from test_verification import VerificationTestCase
from test_actl import model_setting

from actl import tasks


@unittest.skipUnless(shutil.which("git"), "git is required")
class RepairTestCase(VerificationTestCase):
    """A project with worker, reviewer, and escalation models configured."""

    # -- helpers ----------------------------------------------------------

    def major(
        self,
        description="Blocking problem.",
        scenario="the failure happens",
        impact="the task is broken",
        location="src/feature.py",
    ):
        return {
            "decision": "changes_requested",
            "findings": [{
                "severity": "major",
                "description": description,
                "scenario": scenario,
                "impact": impact,
                "location": location,
            }],
        }

    def approved(self):
        return {"decision": "approved", "findings": []}

    def set_reviewer(self, *verdicts):
        """Give the fake reviewer one verdict per invocation."""
        self.env["ACTL_FAKE_REVIEW_SEQUENCE"] = json.dumps(list(verdicts))
        self.env["ACTL_FAKE_REVIEW_COUNT_FILE"] = str(self.base / "review-count")

    def set_worker(self, *behaviors):
        """Give the fake worker one behavior per invocation."""
        self.env["ACTL_FAKE_RUN_SEQUENCE"] = json.dumps(list(behaviors))
        self.env["ACTL_FAKE_RUN_COUNT_FILE"] = str(self.base / "worker-count")

    def worker_models(self):
        return [
            call["argv"][call["argv"].index("--model") + 1]
            for call in self.worker_calls()
        ]

    def attempt_record(self):
        path = self.repo / ".actl" / "attempts" / "t1"
        self.assertTrue(path.is_file(), "no attempt record was written")
        return json.loads(path.read_text(encoding="utf-8"))


class BasicRepairTests(RepairTestCase):
    def test_first_attempt_approved_has_no_retry(self):
        self.make_task("Add the feature")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertEqual(len(self.reviewer_calls()), 1)
        self.assertIn("Worker attempt 1/3 (provider/model)", result.stdout)
        self.assertNotIn("Worker attempt 2/3", result.stdout)
        self.assertEqual(self.attempt_record()["attempts"], 1)

    def test_minor_findings_approve_without_a_repair(self):
        self.set_reviewer({
            "decision": "approved",
            "findings": [{"severity": "minor", "description": "A small thing."}],
        })
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertEqual(len(self.reviewer_calls()), 1)
        self.assertNotIn("Worker attempt 2/3", result.stdout)

    def test_major_findings_trigger_a_second_attempt_with_feedback(self):
        self.set_reviewer(
            self.major(
                "Authentication accepts an expired token.",
                scenario="An expired JWT is accepted by the verification"
                         " function.",
                impact="Users can authenticate with expired credentials.",
                location="src/auth.py",
            ),
            self.approved(),
        )
        self.make_task("Add authentication")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(self.worker_models(), ["provider/model", "provider/model"])
        self.assertEqual(len(self.reviewer_calls()), 2)
        self.assertIn("Review requested changes (1 major finding)", result.stdout)
        self.assertIn("Worker attempt 2/3 (provider/model)", result.stdout)

        # The repair worker gets the original requirements, the blocking
        # findings with their scenario and impact, and a no-reset instruction.
        prompt = self.worker_calls()[1]["argv"][-1]
        self.assertIn("Add authentication", prompt)
        self.assertIn("worker attempt 2 of 3", prompt)
        self.assertIn("Authentication accepts an expired token.", prompt)
        self.assertIn("An expired JWT is accepted", prompt)
        self.assertIn("Users can authenticate with expired credentials.", prompt)
        self.assertIn("src/auth.py", prompt)
        self.assertIn("continue from the existing worktree", prompt)
        self.assertIn("Do not reset the branch", prompt)

        # Both review rounds evaluated the complete implementation.
        for call in self.reviewer_calls():
            self.assertIn("worker-output.txt", call["argv"][-1])

    def test_second_rejection_escalates_and_keeps_the_worktree(self):
        self.set_worker("commit-index", "commit-index", "commit-index")
        self.set_reviewer(
            self.major("First blocking problem."),
            self.major("Second blocking problem."),
            self.approved(),
        )
        self.make_task("Add the feature")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(
            self.worker_models(),
            ["provider/model", "provider/model", "escalation/model"],
        )
        self.assertIn("Worker attempt 3/3 (escalation/model)", result.stdout)

        # The escalation worker receives the newest feedback in full.
        prompt = self.worker_calls()[2]["argv"][-1]
        self.assertIn("worker attempt 3 of 3", prompt)
        self.assertIn("Second blocking problem.", prompt)
        self.assertIn("the failure happens", prompt)
        self.assertIn("the task is broken", prompt)
        self.assertIn("src/feature.py", prompt)

        # Every attempt ran in the same worktree and the earlier commits
        # were preserved, never reset or discarded.
        worktrees = {call["cwd"] for call in self.worker_calls()}
        self.assertEqual(len(worktrees), 1)
        self.assertEqual(
            self.git("status", "--porcelain", cwd=self.worktree()).stdout, ""
        )
        subjects = self.git("log", "--format=%s", "actl/t1").stdout.splitlines()
        self.assertEqual(subjects.count("worker: implement task"), 3)

        # The final review saw the complete cumulative implementation.
        final_prompt = self.reviewer_calls()[2]["argv"][-1]
        for name in (
            "worker-output-1.txt", "worker-output-2.txt", "worker-output-3.txt",
        ):
            self.assertIn(name, final_prompt)

    def test_third_rejection_needs_human_and_stops(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.major("Still broken."))
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertEqual(len(self.worker_calls()), 3)
        self.assertEqual(len(self.reviewer_calls()), 3)
        self.assertIn("Task t1 \u2192 NEEDS_HUMAN", result.stdout)
        self.assertIn("needs human intervention", result.stderr)

        record = self.attempt_record()
        self.assertEqual(record["attempts"], 3)
        self.assertIn("Still broken.", record["feedback"])

        # Every verdict is stored for the human looking at the task.
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            decisions = [
                row["decision"]
                for row in conn.execute(
                    "SELECT decision FROM reviews WHERE task_id = 1 ORDER BY id"
                )
            ]
        finally:
            conn.close()
        self.assertEqual(decisions, ["changes_requested"] * 3)

        # Restarting actl never restarts the budget automatically.
        again = self.run_task("t1")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("NEEDS_HUMAN", again.stderr)
        self.assertEqual(len(self.worker_calls()), 3)
        self.assertEqual(len(self.reviewer_calls()), 3)

    def test_missing_escalation_model_fails_before_any_work(self):
        self.remove_escalation_model()
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no escalation worker model", result.stderr)
        self.assertIn("actl init --escalation-worker-model", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertEqual(self.worker_calls(), [], "no worker may start")
        self.assertEqual(self.reviewer_calls(), [], "no reviewer may start")
        self.assertFalse((self.repo / ".actl" / "worktrees").exists())
        self.assertEqual(
            list((self.repo / ".actl" / "runs").glob("t1-*.log")), [],
            "no stage may start without an escalation model",
        )
        self.assertFalse(
            (self.repo / ".actl" / "execution.lock").exists(),
            "the lock must not be taken",
        )

    def test_missing_escalation_model_precedes_the_execution_lock(self):
        self.remove_escalation_model()
        self.make_task()
        lock_path = self.repo / ".actl" / "execution.lock"
        handle = open(lock_path, "a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = self.run_task("t1")
        finally:
            handle.close()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no escalation worker model", result.stderr)
        self.assertNotIn("already running", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertEqual(self.calls(), [])

    def test_worker_error_stops_without_a_repair(self):
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertEqual(self.reviewer_calls(), [])
        self.assertFalse((self.repo / ".actl" / "attempts" / "t1").exists())

    def test_reviewer_error_stops_without_a_repair(self):
        self.env["ACTL_FAKE_REVIEW_EXIT"] = "2"
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(len(self.worker_calls()), 1, "no implementation retry")
        self.assertEqual(len(self.reviewer_calls()), 1)
        self.assertEqual(self.attempt_record()["attempts"], 1)
        self.assertEqual(self.attempt_record()["feedback"], "")


class ValidationRepairTests(RepairTestCase):
    def test_failed_validation_triggers_a_repair_attempt(self):
        self.set_worker("commit-index", "commit-index")
        self.set_validation("test -f worker-output-2.txt")
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), 2)
        self.assertEqual(len(self.validation_logs()), 2)
        self.assertIn(
            "Validation failed (test -f worker-output-2.txt, exit code 1)",
            result.stdout,
        )
        self.assertIn("Validation passed", result.stdout)

        # The repair worker gets the failed command and its output.
        prompt = self.worker_calls()[1]["argv"][-1]
        self.assertIn("worker attempt 2 of 3", prompt)
        self.assertIn("Failed command: test -f worker-output-2.txt", prompt)
        self.assertIn("Exit code: 1", prompt)
        self.assertIn("Relevant validation output", prompt)

        self.assertIn(
            "result: FAILED",
            self.validation_logs()[0].read_text(encoding="utf-8"),
        )
        self.assertIn(
            "result: PASSED",
            self.validation_logs()[1].read_text(encoding="utf-8"),
        )

    def test_validation_failure_on_all_attempts_needs_human(self):
        self.set_validation("exit 7")
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertEqual(len(self.worker_calls()), 3)
        self.assertEqual(len(self.validation_logs()), 3)
        self.assertEqual(self.reviewer_calls(), [], "review must never start")
        self.assertIn("needs human intervention", result.stderr)

    def test_validation_integrity_failure_is_not_repaired(self):
        # A validation command that changes tracked files is a project
        # configuration problem, not an implementation rejection.
        self.set_validation("echo tampered > worker-output.txt")
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertEqual(self.reviewer_calls(), [])
        self.assertIn("modified tracked files", result.stderr)


class InfrastructureRetryTests(RepairTestCase):
    """Reviewer infrastructure failures never spend an implementation attempt."""

    def test_prose_before_a_verdict_is_reformatted_without_losing_findings(self):
        verdict = self.major(
            "Response cleanup remains cancellable.",
            scenario="Cancel stream.aclose() while response cleanup suspends.",
            impact="The upstream connection remains open.",
            location="src/rag/providers/llm_providers.py:189-209",
        )
        self.set_reviewer(
            "All 35 provider tests pass. Cleanup still has a cancellation gap.\n\n"
            + json.dumps(verdict),
            verdict,
            self.approved(),
        )
        self.set_validation("echo check")
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), 2)
        self.assertEqual(len(self.reviewer_calls()), 3)
        self.assertEqual(len(self.validation_logs()), 2)
        self.assertEqual(self.attempt_record()["attempts"], 2)

        original, correction, _ = self.reviewer_calls()
        self.assertEqual(original["cwd"], correction["cwd"])
        self.assertEqual(
            original["argv"][original["argv"].index("--session") + 1],
            correction["argv"][correction["argv"].index("--session") + 1],
        )
        prompt = correction["argv"][-1]
        self.assertIn("Preserve the decision and every finding", prompt)
        self.assertIn("Do not use tools", prompt)
        self.assertIn("exactly one JSON object", prompt)
        self.assertIn("Reviewer output format invalid; retrying once", result.stdout)

        # Only the implementation rejection triggers a worker repair, and
        # every part of the blocking finding survives in the worker feedback.
        feedback = self.worker_calls()[1]["argv"][-1]
        for field, value in verdict["findings"][0].items():
            self.assertIn(value, feedback, field)
        log = self.review_logs()[0].read_text(encoding="utf-8")
        self.assertIn("format_retry: 1 of 1", log)
        self.assertIn("decision: changes_requested", log)
        self.assertNotIn("result: FAILED", log)
        self.assertFalse(Path(correction["cwd"]).exists())

    def test_format_retry_can_approve_without_another_worker_attempt(self):
        self.set_reviewer(
            'Here is my verdict: {"decision":"approved","findings":[]}',
            self.approved(),
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertEqual(len(self.reviewer_calls()), 2)
        self.assertEqual(self.attempt_record()["attempts"], 1)
        self.assertEqual(self.latest_review()["findings"], [])

    def test_format_retry_is_bounded(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = "not a JSON verdict"
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(len(self.reviewer_calls()), 2)
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertEqual(self.attempt_record()["attempts"], 1)
        self.assertEqual(self.attempt_record()["feedback"], "")
        self.assertIsNone(self.latest_review())
        self.assertIn("format_retry: 1 of 1", self.review_log())
        self.assertIn("result: FAILED", self.review_log())

    def test_empty_format_retry_does_not_reuse_the_previous_response(self):
        self.set_reviewer("not a JSON verdict", "")
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("produced no text output", result.stderr)
        self.assertEqual(len(self.reviewer_calls()), 2)
        self.assertIsNone(self.latest_review())

    def test_semantically_invalid_verdict_is_not_reformatted(self):
        verdict = self.major()
        verdict["decision"] = "approved"
        self.set_reviewer(verdict, self.approved())
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("approved verdict contains", result.stderr)
        self.assertEqual(len(self.reviewer_calls()), 1)
        self.assertIsNone(self.latest_review())

    def test_semantically_invalid_correction_is_refused(self):
        verdict = self.major()
        verdict["decision"] = "approved"
        self.set_reviewer("not a JSON verdict", verdict, self.approved())
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("approved verdict contains", result.stderr)
        self.assertEqual(len(self.reviewer_calls()), 2)
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertIsNone(self.latest_review())

    def test_reviewer_error_retry_does_not_spend_a_worker_attempt(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = "not a JSON verdict"
        self.make_task()
        first = self.run_task("t1")
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(len(self.worker_calls()), 1)
        record = self.attempt_record()
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["feedback"], "")

        # Fixing the reviewer re-validates and re-reviews the same commit:
        # no new worker attempt is spent.
        self.set_reviewer(self.approved())
        second = self.run_task("t1")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertEqual(len(self.reviewer_calls()), 3)
        self.assertIn("Re-verifying task t1", second.stdout)
        self.assertNotIn("Worker attempt 2/3", second.stdout)

    def test_reviewer_error_retry_repeats_validation(self):
        self.set_validation("echo check")
        self.env["ACTL_FAKE_REVIEW_EXIT"] = "2"
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(len(self.validation_logs()), 1)
        self.assertEqual(self.task_status(), "FAILED")

        self.env["ACTL_FAKE_REVIEW_EXIT"] = "0"
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.validation_logs()), 2, "validation must rerun")
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertEqual(self.task_status(), "STAGED")

    def test_escalation_receives_the_newest_feedback_after_an_error(self):
        self.set_worker("commit-index", "commit-index", "commit-index")
        self.set_reviewer(
            self.major("First finding."),
            "not a JSON verdict",
            "still not a JSON verdict",
            self.major("Second finding."),
            self.approved(),
        )
        self.make_task()
        first = self.run_task("t1")
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertEqual(self.worker_models(), ["provider/model"] * 2)

        # The retry re-verifies attempt 2's commit; its rejection feeds the
        # escalation worker instead of leaving it without feedback.
        second = self.run_task("t1")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(
            self.worker_models(),
            ["provider/model", "provider/model", "escalation/model"],
        )
        prompt = self.worker_calls()[2]["argv"][-1]
        self.assertIn("Second finding.", prompt)
        self.assertNotIn("No validation or review feedback is available", prompt)


class PersistenceTests(RepairTestCase):
    def test_recorded_budget_continues_across_invocations(self):
        self.make_task()
        # Simulate an interrupted repair: one attempt and its rejection were
        # already recorded, and the task was left RUNNING before attempt 2.
        attempts_dir = self.repo / ".actl" / "attempts"
        attempts_dir.mkdir(parents=True)
        (attempts_dir / "t1").write_text(
            json.dumps({
                "attempts": 1,
                "feedback": "The independent reviewer rejected the previous"
                            " implementation: Persisted finding.",
            }),
            encoding="utf-8",
        )
        self.set_task_status("RUNNING")
        self.set_reviewer(self.approved())
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("left RUNNING by an interrupted execution", result.stdout)
        self.assertIn("Worker attempt 2/3 (provider/model)", result.stdout)
        prompt = self.worker_calls()[0]["argv"][-1]
        self.assertIn("worker attempt 2 of 3", prompt)
        self.assertIn("Persisted finding.", prompt)

    def test_interrupted_repair_resumes_without_resetting_the_budget(self):
        release = self.base / "worker2.release"
        self.set_worker(
            "commit",
            {"behavior": "commit", "wait_for": str(release)},
            "commit",
        )
        self.set_reviewer(self.major("Persisted finding."), self.approved())
        self.make_task()

        parent = subprocess.Popen(
            [sys.executable, "-m", "actl", "task", "run", "t1"],
            cwd=str(self.repo), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        count_file = self.base / "worker-count"
        try:
            deadline = time.time() + 30
            started = False
            while time.time() < deadline:
                try:
                    started = int(count_file.read_text(encoding="utf-8")) >= 2
                except (OSError, ValueError):
                    started = False
                if started:
                    break
                time.sleep(0.05)
            self.assertTrue(started, "the second worker attempt never started")
        finally:
            parent.kill()
            parent.wait(timeout=30)

        # The interrupted invocation recorded the spent attempt and the
        # rejection feedback before launching the waiting worker.
        record = self.attempt_record()
        self.assertEqual(record["attempts"], 1)
        self.assertIn("Persisted finding.", record["feedback"])

        release.write_text("go", encoding="utf-8")
        self.assertTrue(self.wait_for_lock_release(), "the worker kept the lock")

        # The next invocation continues at attempt 2 with the feedback; it
        # never resets to attempt 1, and the interrupted attempt is retried
        # under the same number instead of spending a new one.
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Worker attempt 2/3 (provider/model)", result.stdout)
        self.assertIn("Persisted finding.", self.worker_calls()[-1]["argv"][-1])
        # The interrupted worker's commit was preserved and the retry
        # continued on top of it.
        subjects = self.git("log", "--format=%s", "actl/t1").stdout.splitlines()
        self.assertEqual(subjects.count("worker: implement task"), 3)
        self.assertEqual(self.attempt_record()["attempts"], 2)

    def test_needs_human_restarts_only_after_the_record_is_removed(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.major("Broken."))
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        workers = len(self.worker_calls())

        # Deleting the attempt record is the explicit human intervention
        # that grants a fresh budget; the existing commits are reused.
        (self.repo / ".actl" / "attempts" / "t1").unlink()
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.approved())
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), workers + 1)
        self.assertIn("Worker attempt 1/3 (provider/model)", result.stdout)

    def test_corrupt_attempt_record_fails_instead_of_resetting(self):
        self.make_task()
        attempts_dir = self.repo / ".actl" / "attempts"
        attempts_dir.mkdir(parents=True)
        (attempts_dir / "t1").write_text("{not json", encoding="utf-8")
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attempt record", result.stderr)
        self.assertIn("fix or remove", result.stderr)
        self.assertEqual(self.worker_calls(), [], "no worker may start")
        self.assertEqual(self.task_status(), "FAILED")

    def test_stage5_needs_changes_continues_from_remaining_attempts(self):
        self.make_task("Add the feature")
        # A Stage 5 task: one worker attempt was rejected by review and the
        # task was left NEEDS_CHANGES with the findings stored.
        self.set_task_status("NEEDS_CHANGES")
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            tasks.record_review(conn, 1, self.base_sha, "changes_requested", [{
                "severity": "major",
                "description": "Legacy finding.",
                "scenario": "legacy scenario",
                "impact": "legacy impact",
                "location": "legacy.py",
            }])
        finally:
            conn.close()

        self.set_reviewer(self.approved())
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Worker attempt 2/3 (provider/model)", result.stdout)
        prompt = self.worker_calls()[0]["argv"][-1]
        self.assertIn("worker attempt 2 of 3", prompt)
        self.assertIn("Legacy finding.", prompt)
        self.assertIn("legacy scenario", prompt)

    def test_stage5_needs_changes_worker_failure_keeps_the_spent_attempt(self):
        self.make_task()
        self.set_task_status("NEEDS_CHANGES")
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            tasks.record_review(conn, 1, self.base_sha, "changes_requested", [{
                "severity": "major",
                "description": "Legacy finding.",
                "scenario": "legacy scenario",
                "impact": "legacy impact",
                "location": "legacy.py",
            }])
        finally:
            conn.close()

        # The first run fails at the worker: the derived attempt baseline is
        # recorded before the worker starts and survives the failure.
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        record = self.attempt_record()
        self.assertEqual(record["attempts"], 1)
        self.assertIn("Legacy finding.", record["feedback"])

        # The retry stays at attempt 2 instead of restarting the budget.
        self.env["ACTL_FAKE_RUN_EXIT"] = "0"
        self.set_reviewer(self.approved())
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Worker attempt 2/3 (provider/model)", result.stdout)


class RepairLogTests(RepairTestCase):
    def test_every_attempt_keeps_its_own_logs(self):
        self.set_validation("echo check")
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.major("Broken."))
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")

        worker_logs = self.worker_logs()
        validation_logs = self.validation_logs()
        review_logs = self.review_logs()
        self.assertEqual(len(worker_logs), 3)
        self.assertEqual(len(validation_logs), 3)
        self.assertEqual(len(review_logs), 3)
        self.assertEqual(
            len(set(worker_logs + validation_logs + review_logs)), 9,
            "every stage of every attempt needs its own log file",
        )

        for index, path in enumerate(worker_logs, start=1):
            text = path.read_text(encoding="utf-8")
            self.assertIn("attempt: " + str(index) + " of 3\n", text)
            self.assertIn("fake opencode: worker turn finished", text)
        for path in validation_logs:
            text = path.read_text(encoding="utf-8")
            self.assertIn("attempt: ", text)
            self.assertIn("result: PASSED", text)
        for path in review_logs:
            text = path.read_text(encoding="utf-8")
            self.assertIn("decision: changes_requested", text)

        self.assertIn("validation log: ", result.stdout)
        self.assertIn("review log: ", result.stdout)
        self.assertIn("log: ", result.stdout)


class EscalationModelConfigTests(RepairTestCase):
    def test_escalation_model_is_stored_updated_and_preserved(self):
        self.assertEqual(
            model_setting(self.metadata()["escalation_worker_model"]), "escalation/model"
        )
        result = self.run_actl(
            "init", "--escalation-worker-model", "other/stronger#high"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the escalation worker model", result.stdout)
        self.assertEqual(
            model_setting(self.metadata()["escalation_worker_model"]), "other/stronger#high"
        )

        # A plain init never removes the configured escalation model.
        self.assertEqual(self.run_actl("init").returncode, 0)
        self.assertEqual(
            model_setting(self.metadata()["escalation_worker_model"]), "other/stronger#high"
        )

    def test_invalid_escalation_model_value_in_metadata_is_rejected(self):
        metadata = self.metadata()
        metadata["escalation_worker_model"] = 42
        self.write_metadata(metadata)
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid 'escalation_worker_model'", result.stderr)
        self.assertEqual(self.worker_calls(), [])

    def test_invalid_escalation_model_is_rejected(self):
        for value in ("", "model", "provider/", "/model", "two words/model"):
            with self.subTest(value=value):
                result = self.run_actl(
                    "init", "--escalation-worker-model", value
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid escalation worker model", result.stderr)
        self.assertEqual(
            model_setting(self.metadata()["escalation_worker_model"]), "escalation/model"
        )


class TaskResetTests(RepairTestCase):
    """``actl task reset`` discards the implementation and the budget."""

    def task_branch_exists(self, task_id="t1") -> bool:
        return (
            self.git(
                "show-ref", "--verify", "refs/heads/actl/" + task_id, check=False
            ).returncode
            == 0
        )

    def test_reset_discards_branch_worktree_and_budget(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.major("Broken."))
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertTrue(self.worktree().is_dir())
        self.assertTrue(self.task_branch_exists())
        self.assertTrue((self.repo / ".actl" / "base" / "t1").is_file())

        result = self.run_actl("task", "reset", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reset task t1", result.stdout)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertFalse(self.worktree().exists())
        self.assertFalse((self.repo / ".actl" / "attempts" / "t1").exists())
        self.assertFalse((self.repo / ".actl" / "base" / "t1").exists())
        self.assertFalse(self.task_branch_exists())
        # The run branch and the base branch are untouched.
        self.assertEqual(
            self.rev("actl/run/r1"), self.base_sha, "the run branch is untouched"
        )
        self.assertEqual(self.rev("main"), self.base_sha)

        # A fresh execution starts over from attempt 1 on a new branch.
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.approved())
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Worker attempt 1/3 (provider/model)", result.stdout)

    def test_reset_discards_the_recorded_test_generation_outcome(self):
        # The test-generation outcome record (.actl/testgen/tN) belongs to
        # the branch and worktree a reset discards: the recorded decision
        # is stale on a fresh branch, so it goes with them, and the next
        # execution runs test generation again instead of skipping a stage
        # that never ran there.
        marker = self.repo / ".actl" / "testgen" / "t1"
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.major("Broken."))
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8")),
            {"decision": "no_tests_needed"},
        )
        generations = len(self.generation_calls())

        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.approved())
        result = self.run_actl("task", "reset", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reset task t1", result.stdout)
        self.assertFalse(marker.exists())

        # The next execution runs test generation again on the fresh branch.
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(
            len(self.generation_calls()), generations + 1,
            "the next execution must run test generation again",
        )
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8")),
            {"decision": "no_tests_needed"},
        )

    def test_failed_worktree_cleanup_keeps_the_budget_and_the_state(self):
        # A locked worktree cannot be removed, so the reset fails during its
        # cleanup. No task state may be discarded before that succeeds: the
        # budget, the stored state, the branch, and the worktree all survive
        # a failed reset, so it can only be retried, never half-done.
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.major("Broken."))
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        record = self.attempt_record()
        self.assertEqual(record["attempts"], 3)
        review = self.latest_review()

        self.git("worktree", "lock", str(self.worktree()))
        result = self.run_actl("task", "reset", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot remove the task worktree", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        # The attempt budget, the stored review, the status, the branch, and
        # the worktree are all exactly as they were before the failed reset.
        self.assertEqual(self.attempt_record(), record)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertEqual(self.latest_review(), review)
        self.assertTrue(self.task_branch_exists())
        self.assertTrue(self.worktree().is_dir())
        self.assertTrue((self.repo / ".actl" / "base" / "t1").is_file())

        # With the worktree removable again, the retry succeeds as before.
        self.git("worktree", "unlock", str(self.worktree()))
        result = self.run_actl("task", "reset", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertFalse((self.repo / ".actl" / "attempts" / "t1").exists())

    def test_reset_of_a_staged_task_is_refused(self):
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "STAGED")

        result = self.run_actl("task", "reset", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("STAGED", result.stderr)
        self.assertIn("actl run merge", result.stderr)
        # Nothing was discarded.
        self.assertEqual(self.task_status(), "STAGED")
        self.assertTrue(self.worktree().is_dir())
        self.assertTrue(self.task_branch_exists())

    def test_reset_refuses_while_an_execution_holds_the_lock(self):
        self.make_task()
        lock_path = self.repo / ".actl" / "execution.lock"
        handle = open(lock_path, "a+b")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = self.run_actl("task", "reset", "t1")
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already running", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")

    def test_reset_never_touches_another_task(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.major("Broken."))
        self.make_task("First")
        self.assertEqual(
            self.run_actl("task", "create", "--title", "Second").returncode, 0
        )
        self.set_task_status("NEEDS_CHANGES", task_id=2)
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status("t1"), "NEEDS_HUMAN")
        self.assertEqual(self.task_status("t2"), "NEEDS_CHANGES")

        result = self.run_actl("task", "reset", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status("t1"), "PENDING")
        self.assertEqual(self.task_status("t2"), "NEEDS_CHANGES")


class TaskReviewTests(RepairTestCase):
    """``actl task review`` re-verifies a hand-fixed NEEDS_HUMAN task."""

    def exhaust(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.major("Broken."))
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        workers = len(self.worker_calls())
        reviews = len(self.reviewer_calls())
        return workers, reviews

    def test_approval_stages_the_task_without_a_worker(self):
        workers, reviews = self.exhaust()
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps(self.approved())
        result = self.run_actl("task", "review", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Task t1 \u2192 STAGED", result.stdout)
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviews + 1)

    def test_rejection_leaves_the_task_needs_human(self):
        workers, reviews = self.exhaust()
        result = self.run_actl("task", "review", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertIn("Task t1 \u2192 NEEDS_HUMAN", result.stdout)
        self.assertIn("was not approved", result.stderr)
        self.assertEqual(len(self.worker_calls()), workers)
        self.assertEqual(len(self.reviewer_calls()), reviews + 1)

    def test_pending_task_is_refused(self):
        self.make_task()
        result = self.run_actl("task", "review", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot review task t1", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")


if __name__ == "__main__":
    unittest.main()
