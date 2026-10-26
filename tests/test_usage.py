"""End-to-end tests for per-invocation model usage capture.

The fake ``opencode`` reports configurable session usage (``GET
/api/session/<id>``) and model pricing metadata (``GET /api/model``), so
these tests verify that worker attempts, reviews, and review-format retries
each record their own tokens and cost, that the records survive CLI
invocations, and that missing data is never mistaken for zero. The worker and
reviewer transcripts and the terminal output stay exactly as before.
"""

from __future__ import annotations

import json
import shutil
import unittest

from test_verification import VerificationTestCase

from actl import tasks


def token_counts(input, output=0, reasoning=0, cache_read=0, cache_write=0):
    return {
        "input": input,
        "output": output,
        "reasoning": reasoning,
        "cache": {"read": cache_read, "write": cache_write},
    }


# Pricing metadata for the models TaskRunTestCase configures: the worker
# ``provider/model``, the reviewer ``reviewer/model``, and the escalation
# ``escalation/model``.
PRICING = [
    {"providerID": "provider", "modelID": "model", "variants": [],
     "cost": [{"input": 3.0, "output": 15.0,
               "cache": {"read": 0.3, "write": 3.75}}]},
    {"providerID": "reviewer", "modelID": "model", "variants": [],
     "cost": [{"input": 1.0, "output": 2.0,
               "cache": {"read": 0.1, "write": 0.2}}]},
    {"providerID": "escalation", "modelID": "model", "variants": [],
     "cost": [{"input": 5.0, "output": 25.0,
               "cache": {"read": 0.5, "write": 6.25}}]},
]


@unittest.skipUnless(shutil.which("git"), "git is required")
class UsageTestCase(VerificationTestCase):
    """A project whose fake OpenCode reports usage and pricing."""

    def set_pricing(self, models=None):
        self.env["ACTL_FAKE_MODEL_LIST"] = json.dumps(
            PRICING if models is None else models
        )

    def set_session_usage(self, cost, counts):
        payload = {"tokens": counts}
        if cost is not None:
            payload["cost"] = cost
        self.env["ACTL_FAKE_SESSION_USAGE"] = json.dumps(payload)

    def set_session_sequence(self, entries, name="usage-count"):
        self.env["ACTL_FAKE_SESSION_USAGE_SEQUENCE"] = json.dumps(entries)
        self.env["ACTL_FAKE_SESSION_USAGE_COUNT_FILE"] = str(self.base / name)

    def usage_rows(self, task_id=1):
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            return tasks.task_usage(conn, task_id)
        finally:
            conn.close()

    def usage_totals(self, task_id=1):
        return tasks.usage_totals(self.usage_rows(task_id))

    def assert_no_usage_on_terminal(self, result):
        for marker in ("input=", "output=", "cache_read=", "usage_status:"):
            self.assertNotIn(marker, result.stdout, "usage leaked to the terminal")


class UsageCaptureTests(UsageTestCase):
    def test_worker_and_reviewer_usage_are_captured(self):
        self.set_pricing()
        self.set_session_usage(0.01, token_counts(1000, output=100))
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_usage_on_terminal(result)

        # The transcripts are unchanged and the token counts are in the logs.
        worker_log = self.worker_logs()[-1].read_text(encoding="utf-8")
        self.assertIn("fake opencode: worker turn finished", worker_log)
        self.assertIn("usage: input=1000 output=100", worker_log)
        self.assertIn("usage_status: available", worker_log)
        review_log = self.review_logs()[-1].read_text(encoding="utf-8")
        self.assertIn("usage: input=1000 output=100", review_log)

        rows = self.usage_rows()
        self.assertEqual([row.stage for row in rows], ["worker", "review"])
        for row in rows:
            self.assertEqual(row.task_id, 1)
            self.assertEqual(row.run_id, 1)
            self.assertEqual(row.usage_status, "available")
            self.assertEqual(row.input_tokens, 1000)
            self.assertEqual(row.output_tokens, 100)
            self.assertTrue(row.cost_complete)
            self.assertEqual(row.cost_source, "reported")
            self.assertAlmostEqual(row.cost_total, 0.01)

    def test_costs_are_derived_from_model_pricing_without_a_reported_cost(self):
        self.set_pricing()
        self.set_session_usage(None, token_counts(1000, output=100))
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)

        worker = self.usage_rows()[0]
        self.assertEqual(worker.cost_source, "derived")
        self.assertAlmostEqual(worker.cost_input, 0.003)
        self.assertAlmostEqual(worker.cost_output, 0.0015)
        self.assertAlmostEqual(worker.cost_total, 0.0045)
        self.assertTrue(worker.cost_complete)
        log = self.worker_logs()[-1].read_text(encoding="utf-8")
        self.assertIn("source=derived complete=yes", log)

    def test_reported_cost_without_pricing_is_incomplete(self):
        # No model list: OpenCode's reported total survives, but the input and
        # output split cannot be derived and is marked incomplete.
        self.set_session_usage(0.02, token_counts(500, output=50))
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)

        worker = self.usage_rows()[0]
        self.assertEqual(worker.usage_status, "available")
        self.assertAlmostEqual(worker.cost_total, 0.02)
        self.assertIsNone(worker.cost_input)
        self.assertIsNone(worker.cost_output)
        self.assertFalse(worker.cost_complete)
        log = self.worker_logs()[-1].read_text(encoding="utf-8")
        self.assertIn("complete=no", log)

    def test_missing_usage_is_unavailable_never_zero(self):
        # No session usage and no pricing: the invocation is recorded, but its
        # tokens and costs are explicitly unavailable rather than zero.
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_usage_on_terminal(result)

        rows = self.usage_rows()
        self.assertEqual([row.stage for row in rows], ["worker", "review"])
        for row in rows:
            self.assertEqual(row.usage_status, "unavailable")
            self.assertIsNone(row.input_tokens)
            self.assertIsNone(row.output_tokens)
            self.assertIsNone(row.cost_total)
            self.assertFalse(row.cost_complete)
        self.assertIn(
            "usage_status: unavailable",
            self.worker_logs()[-1].read_text(encoding="utf-8"),
        )
        totals = self.usage_totals()
        self.assertEqual(totals["count"], 2)
        self.assertIsNone(totals["total"])
        self.assertFalse(totals["complete"])

    def test_retries_record_each_attempt_separately(self):
        self.set_pricing()
        self.env["ACTL_FAKE_REVIEW_SEQUENCE"] = json.dumps([
            {"decision": "changes_requested", "findings": [{
                "severity": "major", "description": "Broken.",
                "scenario": "s", "impact": "i", "location": "f.py",
            }]},
            {"decision": "approved", "findings": []},
        ])
        self.env["ACTL_FAKE_REVIEW_COUNT_FILE"] = str(self.base / "review-count")
        # One lookup per invocation: worker 1, review 1, worker 2, review 2.
        # A worker/reviewer session is fresh, so each value is that
        # invocation's own usage.
        self.set_session_sequence([
            {"cost": 0.01, "tokens": token_counts(1000)},
            {"cost": 0.02, "tokens": token_counts(2000)},
            {"cost": 0.03, "tokens": token_counts(3000)},
            {"cost": 0.04, "tokens": token_counts(4000)},
        ])
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)

        rows = self.usage_rows()
        self.assertEqual(
            [(row.stage, row.attempt) for row in rows],
            [("worker", 1), ("review", 1), ("worker", 2), ("review", 2)],
        )
        self.assertEqual(
            [row.input_tokens for row in rows], [1000, 2000, 3000, 4000]
        )
        totals = self.usage_totals()
        self.assertAlmostEqual(totals["total"], 0.10)
        self.assertTrue(totals["complete"])

    def test_review_format_retry_is_attributed_separately(self):
        self.set_pricing()
        self.env["ACTL_FAKE_REVIEW_SEQUENCE"] = json.dumps([
            "not a JSON verdict",
            {"decision": "approved", "findings": []},
        ])
        self.env["ACTL_FAKE_REVIEW_COUNT_FILE"] = str(self.base / "review-count")
        # The reviewer session is reused for the retry, so its cumulative
        # totals grow: the second lookup's difference is the retry's usage.
        self.set_session_sequence([
            {"cost": 0.01, "tokens": token_counts(1000)},
            {"cost": 0.02, "tokens": token_counts(2000)},
            {"cost": 0.05, "tokens": token_counts(5000)},
        ])
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reviewer output format invalid; retrying once", result.stdout)

        rows = self.usage_rows()
        self.assertEqual(
            [row.stage for row in rows],
            ["worker", "review", "review_format_retry"],
        )
        self.assertEqual(
            [row.input_tokens for row in rows], [1000, 2000, 3000]
        )
        self.assertAlmostEqual(rows[1].cost_total, 0.02)
        self.assertAlmostEqual(rows[2].cost_total, 0.03)

        log = self.review_logs()[-1].read_text(encoding="utf-8")
        self.assertIn("format_retry: 1 of 1", log)
        self.assertEqual(log.count("usage: input="), 2)


class UsagePersistenceTests(UsageTestCase):
    def test_usage_accumulates_across_cli_invocations(self):
        self.set_pricing()
        # First invocation: the worker fails before any review, but its usage
        # is still recorded.
        self.set_session_usage(0.01, token_counts(1000))
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.make_task()
        first = self.run_task("t1")
        self.assertNotEqual(first.returncode, 0)
        workers = [row for row in self.usage_rows() if row.stage == "worker"]
        self.assertEqual(len(workers), 1)
        self.assertAlmostEqual(workers[0].cost_total, 0.01)

        # Second invocation: the worker completes and the reviewer approves.
        del self.env["ACTL_FAKE_RUN_EXIT"]
        self.set_session_usage(0.02, token_counts(2000))
        second = self.run_task("t1")
        self.assertEqual(second.returncode, 0, second.stderr)

        workers = [row for row in self.usage_rows() if row.stage == "worker"]
        self.assertEqual(len(workers), 2, "both invocations keep their usage")
        self.assertEqual(
            [row.input_tokens for row in workers], [1000, 2000]
        )
        self.assertEqual(workers[0].run_id, workers[1].run_id)

    def test_run_usage_is_scoped_to_its_run(self):
        self.set_pricing()
        self.set_session_usage(0.01, token_counts(1000))
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)

        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            run_rows = tasks.run_usage(conn, 1)
            self.assertEqual(len(run_rows), 2)
            self.assertEqual(
                [row.stage for row in run_rows], ["worker", "review"]
            )
            self.assertEqual(tasks.run_usage(conn, 99), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
