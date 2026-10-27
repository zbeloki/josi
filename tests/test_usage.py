"""End-to-end tests for per-invocation model usage capture.

The fake ``opencode`` reports configurable session usage (``GET
/api/session/<id>``) and model pricing metadata (``GET /api/model``), so
these tests verify that test generation, worker attempts, reviews, and
review-format retries each record their own tokens and cost, that the
records survive CLI invocations, and that missing data is never mistaken for
zero. The transcripts and the terminal output stay exactly as before.
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
# ``provider/model``, the reviewer ``reviewer/model``, the escalation
# ``escalation/model``, and the test writer ``tests/model``.
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
    {"providerID": "tests", "modelID": "model", "variants": [],
     "cost": [{"input": 2.0, "output": 10.0,
               "cache": {"read": 0.2, "write": 2.5}}]},
]


@unittest.skipUnless(shutil.which("git"), "git is required")
class UsageTestCase(VerificationTestCase):
    """A project whose fake OpenCode reports usage and pricing."""

    def setUp(self):
        super().setUp()
        # The test-writer stage gets its own named model, so its usage rows
        # and pricing are as uniform as the other stages'.
        result = self.run_actl("init", "--test-writer-model", "tests/model")
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def stage_row(self, stage, task_id=1):
        """Return the single usage record of ``stage`` for the task."""
        rows = [
            row for row in self.usage_rows(task_id) if row.stage == stage
        ]
        self.assertEqual(
            len(rows), 1, "expected one " + stage + " usage record"
        )
        return rows[0]

    def run_usage_rows(self, run_id=1):
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            return tasks.run_usage(conn, run_id)
        finally:
            conn.close()

    def assert_no_usage_on_terminal(self, result):
        for marker in ("input=", "output=", "cache_read=", "usage_status:"):
            self.assertNotIn(marker, result.stdout, "usage leaked to the terminal")

    def cost_field(self, output, name):
        """Return the value of one ``cost_*`` line from ``show`` output."""
        for line in output.splitlines():
            if line.startswith(name + ": "):
                return line.split(": ", 1)[1]
        self.fail(name + " not found in:\n" + output)

    def assert_cost_summary(self, output, records, status):
        """Assert the shown cost summary matches ``records`` and ``status``."""
        def expected(field):
            values = [getattr(row, field) for row in records]
            values = [value for value in values if value is not None]
            return "unavailable" if not values else "%.6f" % sum(values)

        self.assertEqual(self.cost_field(output, "cost_total"), expected("cost_total"))
        self.assertEqual(self.cost_field(output, "cost_input"), expected("cost_input"))
        self.assertEqual(
            self.cost_field(output, "cost_output"), expected("cost_output")
        )
        self.assertEqual(self.cost_field(output, "cost_status"), status)


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
        self.assertEqual(
            [row.stage for row in rows], ["test_writer", "worker", "review"]
        )
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

        worker = self.stage_row("worker")
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

        worker = self.stage_row("worker")
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
        self.assertEqual(
            [row.stage for row in rows], ["test_writer", "worker", "review"]
        )
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
        self.assertEqual(totals["count"], 3)
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
        # One lookup per invocation: test writer, worker 1, review 1,
        # worker 2, review 2. Every session is fresh, so each value is that
        # invocation's own usage.
        self.set_session_sequence([
            {"cost": 0.005, "tokens": token_counts(500)},
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
            [
                ("test_writer", 1), ("worker", 1), ("review", 1),
                ("worker", 2), ("review", 2),
            ],
        )
        self.assertEqual(
            [row.input_tokens for row in rows],
            [500, 1000, 2000, 3000, 4000],
        )
        totals = self.usage_totals()
        self.assertAlmostEqual(totals["total"], 0.105)
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
            {"cost": 0.005, "tokens": token_counts(500)},
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
            ["test_writer", "worker", "review", "review_format_retry"],
        )
        # The retry's tokens are the difference between the two cumulative
        # reviewer lookups (5000 - 2000), not the raw second value.
        self.assertEqual(
            [row.input_tokens for row in rows], [500, 1000, 2000, 3000]
        )
        self.assertAlmostEqual(rows[2].cost_total, 0.02)
        self.assertAlmostEqual(rows[3].cost_total, 0.03)

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
            self.assertEqual(len(run_rows), 3)
            self.assertEqual(
                [row.stage for row in run_rows],
                ["test_writer", "worker", "review"],
            )
            self.assertEqual(tasks.run_usage(conn, 99), [])
        finally:
            conn.close()


class UsageSummaryTests(UsageTestCase):
    """``actl task show`` and ``actl run show`` display accumulated costs."""

    def test_task_show_accumulates_across_retries(self):
        self.set_pricing()
        self.env["ACTL_FAKE_REVIEW_SEQUENCE"] = json.dumps([
            {"decision": "changes_requested", "findings": [{
                "severity": "major", "description": "Broken.",
                "scenario": "s", "impact": "i", "location": "f.py",
            }]},
            {"decision": "approved", "findings": []},
        ])
        self.env["ACTL_FAKE_REVIEW_COUNT_FILE"] = str(self.base / "review-count")
        self.set_session_sequence([
            {"cost": 0.005, "tokens": token_counts(500, output=50)},
            {"cost": 0.01, "tokens": token_counts(1000, output=100)},
            {"cost": 0.02, "tokens": token_counts(2000, output=200)},
            {"cost": 0.03, "tokens": token_counts(3000, output=300)},
            {"cost": 0.04, "tokens": token_counts(4000, output=400)},
        ])
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)

        records = self.usage_rows()
        self.assertEqual([row.stage for row in records], [
            "test_writer", "worker", "review", "worker", "review",
        ])
        shown = self.run_actl("task", "show", "t1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assert_cost_summary(shown.stdout, records, "complete")
        # The task totals are the sum of every attempt, and no per-invocation
        # token count leaks into the summary.
        self.assertAlmostEqual(
            float(self.cost_field(shown.stdout, "cost_total")), 0.105
        )
        self.assert_no_usage_on_terminal(shown)

    def test_task_show_accumulates_across_cli_invocations(self):
        self.set_pricing()
        self.set_session_usage(0.01, token_counts(1000))
        self.env["ACTL_FAKE_RUN_EXIT"] = "1"
        self.make_task()
        self.assertNotEqual(self.run_task("t1").returncode, 0)
        del self.env["ACTL_FAKE_RUN_EXIT"]
        self.set_session_usage(0.02, token_counts(2000))
        self.assertEqual(self.run_task("t1").returncode, 0)

        records = self.usage_rows()
        # The first invocation ran the test writer and a failing worker; the
        # second ran the worker and the review, with the completed
        # test-generation stage skipped.
        self.assertEqual(len(records), 4, "both invocations keep their usage")
        shown = self.run_actl("task", "show", "t1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assert_cost_summary(shown.stdout, records, "complete")
        self.assertAlmostEqual(
            float(self.cost_field(shown.stdout, "cost_total")), 0.06
        )
        self.assert_no_usage_on_terminal(shown)

    def test_run_show_includes_only_its_own_usage(self):
        self.set_pricing()
        self.set_session_usage(0.01, token_counts(1000))
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)
        self.assertEqual(self.integrate("r1").returncode, 0)
        created = self.run_actl("task", "create", "--title", "Second")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.set_session_usage(0.07, token_counts(3000))
        second_run = self.run_task("t2")
        self.assertEqual(second_run.returncode, 0, second_run.stderr)

        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            first_rows = tasks.run_usage(conn, 1)
            second_rows = tasks.run_usage(conn, 2)
        finally:
            conn.close()
        self.assertTrue(first_rows, "the first run has usage")
        self.assertTrue(second_rows, "the second run has usage")
        self.assertEqual(
            {row.task_id for row in first_rows}, {1},
            "the first run only carries its own task's usage",
        )
        self.assertEqual({row.task_id for row in second_rows}, {2})

        first = self.run_actl("run", "show", "r1")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assert_cost_summary(first.stdout, first_rows, "complete")
        self.assertAlmostEqual(
            float(self.cost_field(first.stdout, "cost_total")), 0.03
        )
        second = self.run_actl("run", "show", "r2")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assert_cost_summary(second.stdout, second_rows, "complete")
        self.assertAlmostEqual(
            float(self.cost_field(second.stdout, "cost_total")), 0.21
        )

    def test_show_with_no_recorded_usage_is_not_a_confirmed_zero(self):
        self.make_task()
        conn = tasks.connect(self.repo / ".actl" / "tasks.db")
        try:
            tasks.create_run(conn, "main", self.base_sha, [1])
        finally:
            conn.close()

        shown = self.run_actl("task", "show", "t1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(
            self.cost_field(shown.stdout, "cost_status"), "unavailable"
        )
        self.assertEqual(
            self.cost_field(shown.stdout, "cost_total"), "unavailable"
        )
        self.assertNotIn("0.000000", shown.stdout)

        run_show = self.run_actl("run", "show", "r1")
        self.assertEqual(run_show.returncode, 0, run_show.stderr)
        self.assertEqual(
            self.cost_field(run_show.stdout, "cost_status"), "unavailable"
        )
        self.assertEqual(
            self.cost_field(run_show.stdout, "cost_total"), "unavailable"
        )
        self.assertNotIn("0.000000", run_show.stdout)

    def test_show_reports_unavailable_amounts_as_unavailable(self):
        # No session usage and no pricing: the invocations are recorded with
        # unavailable amounts rather than a confirmed zero.
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)

        records = self.usage_rows()
        shown = self.run_actl("task", "show", "t1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assert_cost_summary(shown.stdout, records, "unavailable")
        self.assertNotIn("0.000000", shown.stdout)

        run_show = self.run_actl("run", "show", "r1")
        self.assertEqual(run_show.returncode, 0, run_show.stderr)
        self.assert_cost_summary(run_show.stdout, self.run_usage_rows(), "unavailable")
        self.assertNotIn("0.000000", run_show.stdout)

    def test_show_reports_partially_known_costs_as_incomplete(self):
        # A reported total without pricing keeps the total but leaves the
        # input/output split unknown, so the summary is marked incomplete.
        self.set_session_usage(0.02, token_counts(500, output=50))
        self.make_task()
        self.assertEqual(self.run_task("t1").returncode, 0)

        shown = self.run_actl("task", "show", "t1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(
            self.cost_field(shown.stdout, "cost_total"), "0.060000"
        )
        self.assertEqual(
            self.cost_field(shown.stdout, "cost_input"), "unavailable"
        )
        self.assertEqual(
            self.cost_field(shown.stdout, "cost_output"), "unavailable"
        )
        self.assertEqual(
            self.cost_field(shown.stdout, "cost_status"), "incomplete"
        )

        run_show = self.run_actl("run", "show", "r1")
        self.assertEqual(
            self.cost_field(run_show.stdout, "cost_status"), "incomplete"
        )


if __name__ == "__main__":
    unittest.main()
