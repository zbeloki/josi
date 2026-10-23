"""End-to-end tests for actl Stage 5: validation and independent review.

Every test runs the CLI as a subprocess against a disposable Git repository
and the fake ``opencode`` executable from ``test_actl``. The fake worker
implements the task; the fake reviewer (``opencode run --agent plan``) prints
a configurable JSON verdict as its ``--format json`` text event, so the suite
needs no real OpenCode process and no model call.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from test_runs import TaskRunTestCase

from actl import tasks


@unittest.skipUnless(shutil.which("git"), "git is required")
class VerificationTestCase(TaskRunTestCase):
    """A project with worker and reviewer models, on a ``main`` base branch."""

    def setUp(self):
        super().setUp()
        result = self.run_actl("init", "--reviewer-model", "reviewer/model")
        self.assertEqual(result.returncode, 0, result.stderr)

    # -- configuration helpers -------------------------------------------

    def metadata(self):
        return json.loads(self.metadata_path().read_text(encoding="utf-8"))

    def write_metadata(self, metadata):
        self.metadata_path().write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )

    def set_validation(self, *commands):
        metadata = self.metadata()
        metadata["validation"] = {"commands": list(commands)}
        self.write_metadata(metadata)

    def remove_reviewer_model(self):
        metadata = self.metadata()
        metadata.pop("reviewer_model", None)
        self.write_metadata(metadata)

    def remove_worker_model(self):
        metadata = self.metadata()
        metadata.pop("worker_model", None)
        self.write_metadata(metadata)

    def remove_escalation_model(self):
        metadata = self.metadata()
        metadata.pop("escalation_worker_model", None)
        self.write_metadata(metadata)

    def dirty_base(self):
        (self.repo / "base-scratch.txt").write_text(
            "dirty\n", encoding="utf-8"
        )

    def clean_base(self):
        path = self.repo / "base-scratch.txt"
        if path.exists():
            path.unlink()

    def set_task_status(self, status, task_id=1):
        """Simulate a Stage 4 task left behind at the given status."""
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            tasks.set_task_status(conn, task_id, status)
        finally:
            conn.close()

    def interrupt_run(self, run_id=1):
        """Leave the run RUNNING, as an interrupted execution would."""
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            tasks.set_run_status(conn, run_id, tasks.RUN_RUNNING)
        finally:
            conn.close()

    # -- call helpers -----------------------------------------------------

    def reviewer_prompt(self):
        calls = self.reviewer_calls()
        self.assertTrue(calls, "the reviewer was never invoked")
        return calls[-1]["argv"][-1]

    # -- log helpers ------------------------------------------------------

    def validation_logs(self):
        return sorted(
            (self.repo / ".actl" / "runs").glob("t1-*-validation.log")
        )

    def review_logs(self):
        return sorted((self.repo / ".actl" / "runs").glob("t1-*-review.log"))

    def worker_logs(self):
        return [
            path for path in self.log_paths()
            if "-validation" not in path.name and "-review" not in path.name
        ]

    def validation_log(self):
        paths = self.validation_logs()
        self.assertTrue(paths, "no validation log was written")
        return paths[-1].read_text(encoding="utf-8")

    def review_log(self):
        paths = self.review_logs()
        self.assertTrue(paths, "no review log was written")
        return paths[-1].read_text(encoding="utf-8")

    # -- storage helpers --------------------------------------------------

    def latest_review(self):
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            return tasks.latest_review(conn, 1)
        finally:
            conn.close()

    def review_json(self, decision, findings):
        return json.dumps({"decision": decision, "findings": findings})


class ValidationTests(VerificationTestCase):
    def test_all_validation_commands_succeed(self):
        self.set_validation("echo first-check", "echo second-check")
        self.make_task("Add the feature")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Validation started (2 commands)", result.stdout)
        self.assertIn("Validation passed", result.stdout)
        self.assertIn("validation log: ", result.stdout)

        log = self.validation_log()
        self.assertLess(
            log.index("$ echo first-check"), log.index("$ echo second-check")
        )
        self.assertIn("first-check", log)
        self.assertIn("second-check", log)
        self.assertIn("exit_code: 0", log)
        self.assertIn("result: PASSED", log)
        self.assertIn("commit: " + self.rev("actl/t1"), log)

        # The reviewer is told what passed.
        prompt = self.reviewer_prompt()
        self.assertIn("echo first-check: exit code 0", prompt)
        self.assertIn("All configured validation commands passed.", prompt)

    def test_failed_validation_command_triggers_repairs_then_needs_human(self):
        # A failed validation command is implementation feedback: all three
        # attempts run and the still-failing command ends in NEEDS_HUMAN.
        self.set_validation("echo first-check", "exit 3", "echo never-runs")
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertIn("Validation failed", result.stdout)
        self.assertIn("exit 3", result.stdout)
        self.assertIn("needs human intervention", result.stderr)
        self.assertIn("log:", result.stderr)
        self.assertEqual(len(self.worker_calls()), 3)
        self.assertEqual(self.reviewer_calls(), [], "review must not start")

        log = self.validation_log()
        self.assertIn("first-check", log)
        self.assertNotIn("never-runs", log)
        self.assertIn("result: FAILED", log)
        self.assertIn("failed_command: exit 3", log)

    def test_validation_modifying_tracked_files_refuses_approval(self):
        self.set_validation("echo tampered > worker-output.txt")
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("modified tracked files", result.stderr)
        self.assertEqual(self.reviewer_calls(), [], "review must not start")
        log = self.validation_log()
        self.assertIn("result: FAILED", log)
        self.assertIn("integrity:", log)

    def test_validation_moving_head_refuses_approval(self):
        self.set_validation(
            "git -c user.name=Validation -c user.email=validation@example.invalid"
            " commit --allow-empty -m 'validation commit'"
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("changed the task commit", result.stderr)
        self.assertEqual(self.reviewer_calls(), [], "review must not start")
        log = self.validation_log()
        self.assertIn("result: FAILED", log)
        self.assertIn("changed the task commit", log)

    def test_validation_modifying_then_restoring_tracked_files_is_refused(self):
        # A real project may format or generate code, validate it, and
        # restore the original: the modified state must never pass.
        (self.repo / "app.py").write_text("VALUE = 0\n", encoding="utf-8")
        (self.repo / "check.py").write_text(
            "from app import VALUE\nassert VALUE == 1\n", encoding="utf-8"
        )
        self.commit("add the app and its acceptance check")
        self.set_validation(
            "printf 'VALUE = 1\\n' > app.py",
            sys.executable + " -B check.py",
            "git restore -- app.py",
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("modified tracked files", result.stderr)
        self.assertEqual(self.reviewer_calls(), [], "review must not start")

        log = self.validation_log()
        self.assertIn("result: FAILED", log)
        self.assertIn("integrity:", log)
        # The check against the modified file never ran, so it cannot hide
        # the change from the integrity check.
        self.assertNotIn("check.py", log)

    def test_no_validation_commands_skips_validation(self):
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Validation skipped (no validation commands configured)",
            result.stdout,
        )
        self.assertEqual(self.validation_logs(), [])
        self.assertIn("Automated validation was skipped", self.reviewer_prompt())
        self.assertEqual(self.task_status(), "STAGED")

    def test_empty_validation_command_list_skips_validation(self):
        self.set_validation()
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Validation skipped (no validation commands configured)",
            result.stdout,
        )
        self.assertEqual(self.validation_logs(), [])

    def test_malformed_validation_config_is_rejected(self):
        self.make_task()
        metadata = self.metadata()
        for value in (
            "oops",
            {"commands": "pytest"},
            {"commands": ["pytest", 3]},
            {"commands": ["   "]},
        ):
            with self.subTest(value=value):
                metadata["validation"] = value
                self.write_metadata(metadata)
                result = self.run_task("t1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("validation", result.stderr)
        self.assertEqual(self.worker_calls(), [], "the worker must not start")
        self.assertEqual(self.task_status(), "PENDING")


class ReviewTests(VerificationTestCase):
    def test_reviewer_approves_without_findings(self):
        self.make_task("Add the feature")
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Reviewer started (model reviewer/model)", result.stdout)
        self.assertIn("Review approved (no findings)", result.stdout)
        self.assertIn("Task t1 \u2192 STAGED (run r1)", result.stdout)
        self.assertIn("review log: ", result.stdout)

        call = self.reviewer_calls()[-1]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--agent") + 1], "plan")
        self.assertEqual(argv[argv.index("--model") + 1], "reviewer/model")
        self.assertEqual(argv[argv.index("--format") + 1], "json")

        log = self.review_log()
        self.assertIn("decision: approved", log)
        self.assertIn("findings: []", log)
        self.assertIn("exit_code: 0", log)

        stored = self.latest_review()
        self.assertEqual(stored["decision"], "approved")
        self.assertEqual(stored["findings"], [])
        self.assertEqual(stored["commit_sha"], self.rev("actl/t1"))

    def test_reviewer_approves_with_minor_findings(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
            "approved",
            [
                {
                    "severity": "minor",
                    "description": "Malformed configuration files may produce"
                                   " an unclear error.",
                },
                {"severity": "minor", "description": "A log line is long."},
            ],
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Review approved (2 minor findings)", result.stdout)
        self.assertIn(
            "Malformed configuration files may produce an unclear error.",
            result.stdout,
        )
        stored = self.latest_review()
        self.assertEqual(stored["decision"], "approved")
        self.assertEqual(len(stored["findings"]), 2)

    def test_reviewer_approves_with_suggestions(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
            "approved",
            [{"severity": "suggestion", "description": "Could extract a helper."}],
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("Review approved (1 suggestion finding)", result.stdout)

    def test_reviewer_rejects_with_major_findings(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
            "changes_requested",
            [{
                "severity": "major",
                "description": "Authentication accepts an expired token.",
                "scenario": "An expired JWT is accepted by the verification"
                            " function.",
                "impact": "Users can authenticate with expired credentials.",
                "location": "src/auth.py",
            }],
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertIn("Review requested changes (1 major finding)", result.stdout)
        self.assertIn("Authentication accepts an expired token.", result.stdout)
        self.assertIn("src/auth.py", result.stdout)
        self.assertIn("needs human intervention", result.stderr)
        self.assertIn("log: ", result.stderr)
        self.assertEqual(len(self.worker_calls()), 3, "repairs run to the limit")

        stored = self.latest_review()
        self.assertEqual(stored["decision"], "changes_requested")
        self.assertEqual(stored["findings"][0]["severity"], "major")
        self.assertEqual(stored["commit_sha"], self.rev("actl/t1"))

    def test_reviewer_rejects_with_critical_findings(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
            "changes_requested",
            [{
                "severity": "critical",
                "description": "The worker can delete the user's data.",
                "scenario": "Running the task deletes every row in the table.",
                "impact": "Permanent data loss.",
                "location": "src/store.py",
            }],
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertIn("Review requested changes (1 critical finding)", result.stdout)

    def test_approval_with_blocking_findings_is_refused(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
            "approved",
            [{
                "severity": "major",
                "description": "Broken under realistic use.",
                "scenario": "scenario",
                "impact": "impact",
                "location": "file.py",
            }],
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("approved verdict contains", result.stderr)
        self.assertIsNone(self.latest_review())

    def test_malformed_reviewer_output_fails_the_task(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = "this is not JSON at all"
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("not one complete JSON verdict", result.stderr)
        self.assertIsNone(self.latest_review())
        log = self.review_log()
        self.assertIn("result: FAILED", log)
        self.assertIn("reason:", log)

    def test_truncated_verdict_after_an_approval_is_refused(self):
        # An echoed approval example followed by an unfinished real verdict
        # must never approve the task.
        self.env["ACTL_FAKE_REVIEW_JSON"] = (
            '{"decision":"approved","findings":[]}\n'
            '{"decision":"changes_requested","findings":['
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("not one complete JSON verdict", result.stderr)
        self.assertIsNone(self.latest_review())

    def test_prose_around_the_verdict_is_refused(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = (
            'Here is my verdict: {"decision":"approved","findings":[]}'
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("not one complete JSON verdict", result.stderr)
        self.assertIsNone(self.latest_review())

    def test_verdict_in_a_code_fence_is_accepted(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = (
            '```json\n{"decision":"approved","findings":[]}\n```'
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")

    def test_unknown_decision_is_refused(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json("maybe", [])
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("unknown review decision", result.stderr)

    def test_unknown_severity_is_refused(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
            "approved",
            [{"severity": "blocker", "description": "Unclear."}],
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("unknown finding severity", result.stderr)

    def test_missing_findings_list_is_refused(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = json.dumps({"decision": "approved"})
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("'findings' must be a list", result.stderr)

    def test_changes_requested_without_blocking_finding_is_refused(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
            "changes_requested",
            [{"severity": "minor", "description": "Only a minor thing."}],
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("no critical or major finding", result.stderr)

    def test_blocking_finding_without_justification_is_refused(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
            "changes_requested",
            [{"severity": "major", "description": "Broken."}],
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("must include", result.stderr)

    def test_reviewer_execution_failure_fails_the_task(self):
        self.env["ACTL_FAKE_REVIEW_EXIT"] = "2"
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("exited with status 2", result.stderr)
        self.assertIsNone(self.latest_review())
        log = self.review_log()
        self.assertIn("result: FAILED", log)
        self.assertIn("exited with status 2", log)

    def test_reviewer_evaluates_the_cumulative_task_diff(self):
        self.make_task("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "STAGED")

        # An interrupted execution left the task at IMPLEMENTED; a human fix
        # is committed on its branch before the run is resumed.
        self.set_task_status("IMPLEMENTED")
        self.interrupt_run()
        worktree = self.worktree()
        (worktree / "second.txt").write_text("second change\n", encoding="utf-8")
        self.commit("second change", cwd=worktree)

        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.reviewer_prompt()
        self.assertIn("worker-output.txt", prompt)
        self.assertIn("second.txt", prompt)
        self.assertIn(self.base_sha, prompt)
        self.assertIn(self.rev("actl/t1"), prompt)

    def test_reviewer_runs_in_an_isolated_worktree(self):
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.reviewer_calls()[-1]
        self.assertNotEqual(call["cwd"], str(self.worktree()))
        self.assertIn(".actl/review", call["cwd"])
        self.assertFalse(
            Path(call["cwd"]).exists(),
            "the temporary reviewer worktree must be removed",
        )
        self.assertTrue(
            self.worktree().is_dir(), "the task worktree must be preserved"
        )
        log = self.review_log()
        self.assertIn(call["cwd"], log)
        self.assertIn("commit: " + self.rev("actl/t1"), log)

    def test_reviewer_checkout_commit_is_refused(self):
        self.env["ACTL_FAKE_REVIEW_MUTATE"] = "commit"
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("changed its checkout", result.stderr)
        self.assertIsNone(self.latest_review())
        self.assertIn("result: FAILED", self.review_log())

        # The real task worktree is untouched and the temporary checkout is
        # removed, so the reviewer commit is discarded with it.
        worktree = self.worktree()
        self.assertEqual(
            (worktree / "worker-output.txt").read_text(encoding="utf-8"),
            "worker change\n",
        )
        self.assertEqual(self.git("status", "--porcelain", cwd=worktree).stdout, "")
        subjects = self.git("log", "--format=%s", "actl/t1").stdout
        self.assertNotIn("reviewer: touch the checkout", subjects)
        self.assertFalse(Path(self.reviewer_calls()[-1]["cwd"]).exists())

    def test_reviewer_checkout_dirty_tracked_files_are_refused(self):
        self.env["ACTL_FAKE_REVIEW_MUTATE"] = "dirty"
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("modified tracked files in its checkout", result.stderr)
        self.assertIsNone(self.latest_review())

    def test_reviewer_untracked_scratch_files_are_allowed(self):
        self.env["ACTL_FAKE_REVIEW_MUTATE"] = "untracked"
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertFalse((self.worktree() / "reviewer-scratch.txt").exists())
        self.assertFalse(Path(self.reviewer_calls()[-1]["cwd"]).exists())

    def test_task_branch_moving_during_review_is_not_approved(self):
        self.make_task()
        marker = self.base / "reviewer.pid"
        release = self.base / "reviewer.release"
        self.env["ACTL_FAKE_REVIEW_MARKER"] = str(marker)
        self.env["ACTL_FAKE_REVIEW_WAIT_FOR"] = str(release)

        parent = subprocess.Popen(
            [sys.executable, "-m", "actl", "task", "run", "t1"],
            cwd=str(self.repo), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertTrue(self.wait_for_path(marker), "the reviewer never started")
            # A human commits in the task worktree while the reviewer runs.
            (self.worktree() / "human.txt").write_text("human\n", encoding="utf-8")
            self.commit("moved during review", cwd=self.worktree())
        finally:
            release.write_text("go", encoding="utf-8")
            try:
                out, err = parent.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                parent.kill()
                out, err = parent.communicate()

        self.assertNotEqual(parent.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("changed the task commit", err)
        self.assertIsNone(self.latest_review())


class PolicyTests(VerificationTestCase):
    def test_builtin_risk_based_policy_is_provided(self):
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        prompt = self.reviewer_prompt()
        for phrase in (
            "CRITICAL",
            "MAJOR",
            "MINOR",
            "SUGGESTION",
            "likelihood and impact",
            "must never block approval",
            "speculative improvements",
            "sufficiently correct and safe",
        ):
            self.assertIn(phrase, prompt)

    def test_project_review_policy_is_included(self):
        (self.repo / "REVIEW_POLICY.md").write_text(
            "# Project policy\n\nRequire a security review for auth changes.\n",
            encoding="utf-8",
        )
        self.commit("add the project review policy")
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        prompt = self.reviewer_prompt()
        self.assertIn("Project review policy (REVIEW_POLICY.md):", prompt)
        self.assertIn("Require a security review for auth changes.", prompt)
        # The built-in rules stay in force next to the project policy.
        self.assertIn("CRITICAL", prompt)
        self.assertIn("must never block approval", prompt)

    def test_without_a_project_policy_only_the_builtin_is_used(self):
        self.assertFalse((self.repo / "REVIEW_POLICY.md").exists())
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        prompt = self.reviewer_prompt()
        self.assertNotIn("Project review policy", prompt)
        self.assertIn("CRITICAL", prompt)

    def test_minor_findings_and_suggestions_do_not_block_approval(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
            "approved",
            [
                {"severity": "minor", "description": "Small edge case."},
                {"severity": "suggestion", "description": "Optional cleanup."},
            ],
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertIn("1 minor, 1 suggestion", result.stdout)

    def test_critical_and_major_findings_block_approval(self):
        for index, severity in enumerate(("critical", "major"), start=1):
            with self.subTest(severity=severity):
                created = self.run_actl(
                    "task", "create", "--title", "Task " + severity
                )
                self.assertEqual(created.returncode, 0, created.stderr)
                task_id = created.stdout.split()[-1]
                self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
                    "changes_requested",
                    [{
                        "severity": severity,
                        "description": "Blocking problem.",
                        "scenario": "scenario",
                        "impact": "impact",
                        "location": "file.py",
                    }],
                )
                result = self.run_task(task_id)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.task_status(task_id), "NEEDS_HUMAN")
                self.assertIn("1 " + severity + " finding", result.stdout)
                # Every rejection triggers a repair: three worker attempts
                # and three review verdicts per task, then NEEDS_HUMAN.
                self.assertEqual(len(self.worker_calls()), 3 * index)
                self.assertEqual(len(self.reviewer_calls()), 3 * index)


class Stage5IntegrationTests(VerificationTestCase):
    def test_worker_and_reviewer_use_their_configured_models(self):
        self.assertEqual(
            self.run_actl(
                "init", "--worker-model", "openrouter/worker#high"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_actl(
                "init", "--reviewer-model", "openrouter/reviewer#low"
            ).returncode,
            0,
        )
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reviewer model: openrouter/reviewer#low", result.stdout)
        worker = self.worker_calls()[-1]["argv"]
        reviewer = self.reviewer_calls()[-1]["argv"]
        self.assertIn("openrouter/worker#high", worker)
        self.assertIn("openrouter/reviewer#low", reviewer)

    def test_implemented_task_is_validated_and_reviewed_without_the_worker(self):
        self.make_task("Add the feature")
        self.set_validation("echo check")
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), 1)

        # An interrupted execution left the task at IMPLEMENTED before it was
        # staged: the run is resumed and the committed implementation is
        # validated and reviewed again without rerunning the worker.
        self.set_task_status("IMPLEMENTED")
        self.rewind_run_to_base()
        self.interrupt_run()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), 1, "the worker must not rerun")
        self.assertEqual(len(self.reviewer_calls()), 2)
        self.assertIn("Reusing worktree", result.stdout)
        self.assertIn("Validation passed", result.stdout)

    def test_implemented_task_is_validated_and_reviewed_without_a_worker_model(self):
        self.make_task("Add the feature")
        self.set_validation("echo check")
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), 1)

        # An interrupted execution left the task at IMPLEMENTED before it was
        # staged, in a project that never configured a worker model: the
        # reviewer model alone is enough to validate and review it.
        self.set_task_status("IMPLEMENTED")
        self.rewind_run_to_base()
        self.interrupt_run()
        self.remove_worker_model()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.task_status(), "STAGED")
        self.assertEqual(len(self.worker_calls()), 1, "the worker must not rerun")
        self.assertEqual(len(self.reviewer_calls()), 2)
        self.assertIn("Validation passed", result.stdout)

    def test_validation_and_review_logs_are_preserved(self):
        self.set_validation("echo checked")
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.worker_logs())
        self.assertTrue(self.validation_logs())
        self.assertTrue(self.review_logs())
        self.assertIn("result: PASSED", self.validation_log())
        self.assertIn("decision: approved", self.review_log())
        self.assertIn(
            "fake opencode: worker turn finished",
            self.worker_logs()[-1].read_text(encoding="utf-8"),
        )
        self.assertIn("validation log: ", result.stdout)
        self.assertIn("review log: ", result.stdout)

    def test_missing_reviewer_model_fails_before_any_work(self):
        self.remove_reviewer_model()
        self.make_task()
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no reviewer model", result.stderr)
        self.assertIn("actl init --reviewer-model", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertEqual(self.worker_calls(), [], "no worker may be launched")
        self.assertEqual(self.reviewer_calls(), [])
        self.assertFalse((self.repo / ".actl" / "worktrees").exists())
        self.assertEqual(
            list((self.repo / ".actl" / "runs").glob("t1-*.log")), [],
            "no stage may start without a reviewer model",
        )
        self.assertFalse(
            (self.repo / ".actl" / "execution.lock").exists(),
            "the lock must not be taken",
        )

    def test_missing_reviewer_model_precedes_the_execution_lock(self):
        # With the lock held elsewhere, the run still reports the missing
        # reviewer model instead of the lock conflict: the refusal happens
        # before the lock is attempted.
        self.remove_reviewer_model()
        self.make_task()
        lock_path = self.repo / ".actl" / "execution.lock"
        handle = open(lock_path, "a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = self.run_task("t1")
        finally:
            handle.close()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no reviewer model", result.stderr)
        self.assertNotIn("already running", result.stderr)
        self.assertEqual(self.task_status(), "PENDING")
        self.assertEqual(self.calls(), [])

    def test_done_task_is_not_run_again(self):
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.task_status(), "DONE")
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already DONE", result.stderr)
        self.assertEqual(len(self.worker_calls()), 1)
        self.assertEqual(len(self.reviewer_calls()), 1)

    def test_needs_human_task_is_not_run_again_without_intervention(self):
        self.env["ACTL_FAKE_REVIEW_JSON"] = self.review_json(
            "changes_requested",
            [{
                "severity": "major",
                "description": "Blocking problem.",
                "scenario": "scenario",
                "impact": "impact",
                "location": "file.py",
            }],
        )
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "NEEDS_HUMAN")
        self.assertEqual(len(self.worker_calls()), 3)

        # The exhausted budget is never restarted automatically.
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NEEDS_HUMAN", result.stderr)
        self.assertIn(".actl/attempts/t1", result.stderr)
        self.assertEqual(len(self.worker_calls()), 3, "no automatic restart")
        self.assertEqual(len(self.reviewer_calls()), 3)

    def test_staging_leaves_the_base_branch_untouched(self):
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "STAGED")
        # Execution stages the approved commit into the run branch only;
        # the base branch moves when the run is integrated explicitly.
        self.assertEqual(self.rev("main"), self.base_sha)
        self.assertEqual(self.rev("actl/run/r1"), self.rev("actl/t1"))
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main"
        )
        self.assertEqual(
            self.git("status", "--porcelain").stdout, "", "main stays clean"
        )
        stored = self.latest_review()
        self.assertEqual(stored["commit_sha"], self.rev("actl/t1"))
        self.assertEqual(stored["commit_sha"], self.rev("HEAD", cwd=self.worktree()))

        self.assertEqual(self.integrate().returncode, 0)
        self.assertEqual(self.task_status(), "DONE")
        self.assertEqual(self.rev("main"), self.rev("actl/t1"))
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main"
        )
        self.assertEqual(
            self.git("status", "--porcelain").stdout, "", "main stays clean"
        )

    def test_reviewer_model_is_stored_and_validated(self):
        metadata = self.metadata()
        self.assertEqual(metadata["reviewer_model"], "reviewer/model")

        result = self.run_actl("init", "--reviewer-model", "provider/other")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the reviewer model", result.stdout)
        self.assertEqual(self.metadata()["reviewer_model"], "provider/other")

        # A plain init keeps the reviewer model.
        self.assertEqual(self.run_actl("init").returncode, 0)
        self.assertEqual(self.metadata()["reviewer_model"], "provider/other")

        for value in ("", "model", "provider/", "/model", "two words/model"):
            with self.subTest(value=value):
                result = self.run_actl("init", "--reviewer-model", value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid reviewer model", result.stderr)

    def test_dirty_worktree_is_refused_before_validation(self):
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "STAGED")
        reviews = len(self.reviewer_calls())

        # An interrupted task at IMPLEMENTED with an uncommitted tracked
        # change must not be validated or reviewed.
        self.set_task_status("IMPLEMENTED")
        self.interrupt_run()
        (self.worktree() / "worker-output.txt").write_text(
            "uncommitted\n", encoding="utf-8"
        )
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("uncommitted changes", result.stderr)
        self.assertEqual(
            len(self.reviewer_calls()), reviews, "review must not start"
        )

    def test_a_reviewed_task_can_still_be_deleted(self):
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertIsNotNone(self.latest_review())

        result = self.run_actl("task", "delete", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Deleted task t1", result.stdout)
        listing = self.run_actl("task", "list")
        self.assertEqual(listing.stdout, "")

    def test_a_deleted_task_branch_is_never_reviewed_empty(self):
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "STAGED")
        reviews = len(self.reviewer_calls())

        # A task whose branch is gone: re-creating it from the run branch
        # must not turn an empty implementation into a reviewed task.
        self.set_task_status("IMPLEMENTED")
        self.interrupt_run()
        self.git("worktree", "remove", "--force", str(self.worktree()))
        self.git("branch", "-D", "actl/t1")
        result = self.run_task("t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.task_status(), "FAILED")
        self.assertIn("no committed changes", result.stderr)
        self.assertEqual(
            len(self.reviewer_calls()), reviews, "review must not start"
        )

    def test_review_uses_the_recorded_branch_base(self):
        self.make_task("Add the feature")
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.task_status(), "STAGED")
        first_commit = self.rev("actl/t1")

        # An interrupted execution left the task at IMPLEMENTED; the
        # project's base branch is reconfigured after the first task commit.
        # The review diff must still start at the commit the branch was
        # created from.
        self.set_task_status("IMPLEMENTED")
        self.interrupt_run()
        self.git("branch", "develop", first_commit)
        (self.worktree() / "second.txt").write_text(
            "second change\n", encoding="utf-8"
        )
        self.commit("second change", cwd=self.worktree())
        self.assertEqual(self.run_actl("init", "--base", "develop").returncode, 0)

        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.repo / ".actl" / "base" / "t1").read_text(encoding="utf-8").strip(),
            self.base_sha,
        )
        prompt = self.reviewer_prompt()
        self.assertIn("created from base commit " + self.base_sha, prompt)
        diff = prompt.split("```diff\n", 1)[1].split("\n```", 1)[0]
        self.assertIn("worker-output.txt", diff)
        self.assertIn("second.txt", diff)


if __name__ == "__main__":
    unittest.main()
