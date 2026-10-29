"""Tests that josi does not generate OpenCode configuration at launch.

OpenCode processes inherit the normal process environment, and an explicitly
supplied environment (including ``OPENCODE_CONFIG_CONTENT``) passes through
unchanged. These tests cover the wrapper directly and the real launch paths
(the planner TUI and the worker/reviewer runs in their worktrees) through the
fake ``opencode`` executable from ``test_josi``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from josi import opencode  # noqa: E402
from test_josi import JosiTestCase  # noqa: E402
from test_runs import TaskRunTestCase  # noqa: E402

INLINE = json.dumps({"provider": {"openrouter": {"body": {"x": 1}}}})


class WrapperEnvironmentTests(unittest.TestCase):
    """Every wrapper launch hands ``env`` to the subprocess untouched."""

    def launches(self, env):
        """Run each launching wrapper; return the env passed to each."""
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        seen = []
        root = Path("/tmp")
        with mock.patch.object(
            opencode.subprocess, "run", return_value=completed
        ) as run:
            opencode.launch_tui("oc", root, "s1", env=env)
            opencode.run_headless(
                "oc", root, "s1", "m", "p", output=None, env=env
            )
            opencode.run_headless(
                "oc", root, "s1", "m", "p", output=None,
                agent=opencode.REVIEWER_AGENT, output_format="json", env=env,
            )
            try:
                opencode._api("oc", root, "GET", "/api/x", env=env)
            except opencode.OpenCodeError:
                pass
            for call in run.call_args_list:
                seen.append(call.kwargs["env"])
        return seen

    def test_no_launch_environment_helper_remains(self):
        for name in ("launch_environment", "existing_launch_config",
                     "DEFAULT_LAUNCH_CONFIG", "OPENCODE_CONFIG_CONTENT"):
            self.assertFalse(hasattr(opencode, name), name)

    def test_omitted_env_inherits_the_process_environment(self):
        for env in self.launches(None):
            self.assertIsNone(env)

    def test_explicit_env_passes_through_unchanged(self):
        explicit = {"KEEP": "yes", "OPENCODE_CONFIG_CONTENT": INLINE}
        envs = self.launches(explicit)
        self.assertEqual(len(envs), 4)
        for env in envs:
            self.assertIs(env, explicit)
            self.assertEqual(env, {"KEEP": "yes",
                                   "OPENCODE_CONFIG_CONTENT": INLINE})


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlannerLaunchConfigTests(JosiTestCase):
    """The planner launch adds no OpenCode configuration."""

    def setUp(self):
        super().setUp()
        self.initialize()

    def test_planner_calls_receive_no_josi_generated_config(self):
        self.assertEqual(self.run_josi("plan").returncode, 0)
        calls = self.calls()
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["env"], {}, call["argv"])

    def test_caller_config_is_not_overwritten_by_the_planner(self):
        self.env["OPENCODE_CONFIG_CONTENT"] = INLINE
        self.env["OPENCODE_CONFIG"] = "/some/custom.json"
        self.assertEqual(self.run_josi("plan").returncode, 0)
        self.assertTrue(self.tui_calls())
        for call in self.calls():
            self.assertEqual(call["env"], {
                "OPENCODE_CONFIG_CONTENT": INLINE,
                "OPENCODE_CONFIG": "/some/custom.json",
            }, call["argv"])


@unittest.skipUnless(shutil.which("git"), "git is required")
class WorktreeLaunchConfigTests(TaskRunTestCase):
    """Worker, reviewer, and escalation launches add no configuration."""

    def init_openrouter_models(self):
        for option, model in (
            ("--worker-model", "openrouter/anthropic/claude-sonnet-latest"),
            ("--reviewer-model", "openrouter/openai/gpt-5"),
            ("--escalation-worker-model", "openrouter/anthropic/claude-opus-latest"),
        ):
            result = self.run_josi("init", option, model)
            self.assertEqual(result.returncode, 0, result.stderr)

    def run_and_check(self, expected_env):
        self.init_openrouter_models()
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        workers = self.worker_calls()
        reviewers = self.reviewer_calls()
        self.assertTrue(workers, "the worker was never launched")
        self.assertTrue(reviewers, "the reviewer was never launched")
        for call in self.calls():
            self.assertEqual(call["env"], expected_env, call["argv"])

    def test_worker_and_reviewer_receive_no_josi_generated_config(self):
        self.run_and_check({})

    def test_project_opencode_json_is_not_read_or_merged(self):
        (self.repo / "opencode.json").write_text(json.dumps({
            "provider": {"openrouter": {"body": {"provider": {"sort": "latency"}}}},
        }), encoding="utf-8")
        self.run_and_check({})

    def test_caller_config_passes_through_to_every_launch_unchanged(self):
        self.env["OPENCODE_CONFIG_CONTENT"] = INLINE
        self.run_and_check({"OPENCODE_CONFIG_CONTENT": INLINE})


if __name__ == "__main__":
    unittest.main()
