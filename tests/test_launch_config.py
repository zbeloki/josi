"""Tests for actl's launch-time OpenCode configuration.

Every OpenCode launch made by actl passes an ``OPENCODE_CONFIG_CONTENT`` that
defaults OpenRouter requests to provider-level price sorting. These tests cover
the configuration builder directly and the real launch paths (the planner TUI
and the worker/reviewer noninteractive runs in their worktrees) through the
fake ``opencode`` executable from ``test_actl``.
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from actl import opencode  # noqa: E402
from test_actl import ActlTestCase  # noqa: E402
from test_runs import TaskRunTestCase  # noqa: E402


def parse_config(environment):
    """Return the object actl passed through ``OPENCODE_CONFIG_CONTENT``."""
    return json.loads(environment[opencode.OPENCODE_CONFIG_CONTENT])


class LaunchEnvironmentTests(unittest.TestCase):
    """Unit tests for :func:`actl.opencode.launch_environment`."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(prefix="actl-launch-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def config(self, env=None):
        environment = opencode.launch_environment(self.root, env or {})
        return parse_config(environment)

    def write_config(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")

    def test_default_sorts_openrouter_providers_by_price(self):
        config = self.config()
        self.assertEqual(
            config["provider"]["openrouter"]["body"]["provider"]["sort"],
            "price",
        )

    def test_default_is_provider_level_without_models_or_provider_list(self):
        config = self.config()
        openrouter = config["provider"]["openrouter"]
        # The default must not name or list models, nor select a specific
        # provider: it only asks OpenRouter to sort providers by price.
        self.assertNotIn("models", openrouter)
        self.assertEqual(set(openrouter["body"]), {"provider"})
        self.assertEqual(openrouter["body"]["provider"], {"sort": "price"})
        # No unrelated provider is configured and no model is pinned.
        self.assertEqual(set(config["provider"]), {"openrouter"})
        self.assertNotIn("model", config)

    def test_project_config_is_preserved_and_overrides_default(self):
        self.write_config("opencode.json", json.dumps({
            "model": "openrouter/anthropic/claude-sonnet-latest",
            "mcp": {"servers": {"demo": {"type": "local"}}},
            "provider": {
                "openrouter": {
                    "body": {"provider": {"sort": "throughput"}},
                },
            },
        }))
        config = self.config()
        self.assertEqual(
            config["provider"]["openrouter"]["body"]["provider"]["sort"],
            "throughput",
        )
        self.assertEqual(
            config["model"], "openrouter/anthropic/claude-sonnet-latest"
        )
        self.assertEqual(config["mcp"]["servers"]["demo"]["type"], "local")

    def test_model_specific_project_override_wins_over_the_default(self):
        self.write_config("opencode.json", json.dumps({
            "provider": {
                "openrouter": {
                    "models": {
                        "anthropic/claude-sonnet-latest": {
                            "body": {"provider": {"sort": "latency"}},
                        },
                    },
                },
            },
        }))
        config = self.config()
        # The project's model-level setting survives next to the default.
        self.assertEqual(
            config["provider"]["openrouter"]["models"][
                "anthropic/claude-sonnet-latest"
            ]["body"]["provider"]["sort"],
            "latency",
        )
        self.assertEqual(
            config["provider"]["openrouter"]["body"]["provider"]["sort"],
            "price",
        )

    def test_directory_config_overrides_an_ancestor_config(self):
        ancestor = self.root / "parent"
        child = ancestor / "project"
        child.mkdir(parents=True)
        (ancestor / "opencode.json").write_text(
            json.dumps({"model": "ancestor/model"}), encoding="utf-8"
        )
        (child / "opencode.json").write_text(
            json.dumps({"model": "child/model"}), encoding="utf-8"
        )
        config = parse_config(
            opencode.launch_environment(child, {})
        )
        self.assertEqual(config["model"], "child/model")

    def test_dot_opencode_config_overrides_a_direct_config(self):
        self.write_config("opencode.json", json.dumps({"model": "direct/model"}))
        self.write_config(
            ".opencode/opencode.json", json.dumps({"model": "nested/model"})
        )
        self.assertEqual(self.config()["model"], "nested/model")

    def test_jsonc_project_config_is_parsed(self):
        self.write_config("opencode.jsonc", """\
{
  // Keep this model and override the default.
  "model": "openrouter/anthropic/claude-sonnet-latest",
  "provider": {
    "openrouter": { "body": { "provider": { "sort": "throughput" } } },
  },
}
""")
        config = self.config()
        self.assertEqual(
            config["model"], "openrouter/anthropic/claude-sonnet-latest"
        )
        self.assertEqual(
            config["provider"]["openrouter"]["body"]["provider"]["sort"],
            "throughput",
        )

    def test_custom_config_is_preserved_below_project_config(self):
        custom = self.root / "custom.json"
        custom.write_text(json.dumps({
            "provider": {"openrouter": {"body": {"provider": {"sort": "latency"}}}},
            "username": "custom",
        }), encoding="utf-8")
        self.write_config(
            "opencode.json",
            json.dumps({"provider": {
                "openrouter": {"body": {"provider": {"sort": "throughput"}}},
            }}),
        )
        config = self.config({opencode.OPENCODE_CONFIG: str(custom)})
        # Project config outranks the custom config, which outranks the default.
        self.assertEqual(
            config["provider"]["openrouter"]["body"]["provider"]["sort"],
            "throughput",
        )
        self.assertEqual(config["username"], "custom")

    def test_existing_inline_content_is_preserved_and_overrides_everything(self):
        existing = json.dumps({
            "provider": {"openrouter": {"body": {"provider": {"sort": "latency"}}}},
            "other": {"kept": True},
        })
        self.write_config(
            "opencode.json",
            json.dumps({"provider": {
                "openrouter": {"body": {"provider": {"sort": "throughput"}}},
            }}),
        )
        config = self.config({opencode.OPENCODE_CONFIG_CONTENT: existing})
        self.assertEqual(
            config["provider"]["openrouter"]["body"]["provider"]["sort"],
            "latency",
        )
        self.assertEqual(config["other"], {"kept": True})

    def test_malformed_project_config_is_ignored(self):
        self.write_config("opencode.json", "{ this is not json")
        # A broken project config must not break a launch; the default applies.
        self.assertEqual(
            self.config()["provider"]["openrouter"]["body"]["provider"]["sort"],
            "price",
        )

    def test_environment_is_returned_with_the_inline_config(self):
        environment = opencode.launch_environment(self.root, {"KEEP": "yes"})
        self.assertEqual(environment["KEEP"], "yes")
        self.assertIn(opencode.OPENCODE_CONFIG_CONTENT, environment)


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlannerLaunchConfigTests(ActlTestCase):
    """The planner TUI launch carries the OpenRouter default."""

    def setUp(self):
        super().setUp()
        self.initialize()

    def test_planner_launch_passes_price_sorted_providers(self):
        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        tui = self.tui_calls()[-1]
        config = parse_config(tui["env"])
        self.assertEqual(
            config["provider"]["openrouter"]["body"]["provider"]["sort"],
            "price",
        )

    def test_every_planner_opencode_call_carries_the_default(self):
        self.assertEqual(self.run_actl("plan").returncode, 0)
        calls = self.calls()
        self.assertTrue(calls)
        for call in calls:
            config = parse_config(call["env"])
            self.assertEqual(
                config["provider"]["openrouter"]["body"]["provider"]["sort"],
                "price",
                call["argv"],
            )


@unittest.skipUnless(shutil.which("git"), "git is required")
class WorktreeLaunchConfigTests(TaskRunTestCase):
    """The worker and reviewer launches in their worktrees carry the default."""

    def init_openrouter_models(self):
        for option, model in (
            ("--worker-model", "openrouter/anthropic/claude-sonnet-latest"),
            ("--reviewer-model", "openrouter/openai/gpt-5"),
            ("--escalation-worker-model", "openrouter/anthropic/claude-opus-latest"),
        ):
            result = self.run_actl("init", option, model)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_and_reviewer_launches_carry_price_sorted_providers(self):
        self.init_openrouter_models()
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)

        workers = self.worker_calls()
        reviewers = self.reviewer_calls()
        self.assertTrue(workers, "the worker was never launched")
        self.assertTrue(reviewers, "the reviewer was never launched")
        # Different OpenRouter models are used, but the provider-level default
        # is identical for both and never names a model.
        for call in workers + reviewers:
            config = parse_config(call["env"])
            openrouter = config["provider"]["openrouter"]
            self.assertEqual(
                openrouter["body"]["provider"]["sort"], "price", call["argv"]
            )
            self.assertNotIn("models", openrouter)

    def test_worktree_launches_preserve_project_configuration(self):
        # An untracked project config above the worktree (the repository root)
        # is discovered like OpenCode discovers it.
        (self.repo / "opencode.json").write_text(json.dumps({
            "model": "openrouter/anthropic/claude-sonnet-latest",
            "mcp": {"servers": {"demo": {"type": "local"}}},
            "provider": {
                "openrouter": {"body": {"provider": {"sort": "throughput"}}},
            },
        }), encoding="utf-8")
        self.init_openrouter_models()
        self.make_task()
        result = self.run_task("t1")
        self.assertEqual(result.returncode, 0, result.stderr)

        for call in self.worker_calls() + self.reviewer_calls():
            config = parse_config(call["env"])
            self.assertEqual(
                config["provider"]["openrouter"]["body"]["provider"]["sort"],
                "throughput",
                call["argv"],
            )
            self.assertEqual(
                config["model"], "openrouter/anthropic/claude-sonnet-latest"
            )
            self.assertEqual(config["mcp"]["servers"]["demo"]["type"], "local")


if __name__ == "__main__":
    unittest.main()
