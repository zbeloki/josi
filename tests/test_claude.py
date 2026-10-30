"""Tests for the Claude Code agent backend.

A fake ``claude`` executable on ``PATH`` stands in for Claude Code: it records
its argument vector, working directory, and stdin, and emulates the
noninteractive ``-p --output-format stream-json`` turns of the worker, the test
writer, and the reviewer. No real Claude process or model call is made.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import unittest

from test_josi import JosiTestCase

from josi import agents, usage

FAKE_CLAUDE = '''#!python
"""Fake Claude Code CLI used by josi's Claude backend tests.

Environment:

* ``JOSI_FAKE_LOG``               - JSONL file receiving argv, cwd, and stdin
* ``JOSI_FAKE_CLAUDE_REVIEW_JSON`` - the reviewer's final message
* ``JOSI_FAKE_CLAUDE_EXIT``       - exit code of noninteractive turns
"""

import json
import os
import subprocess
import sys


def commit(name, text, who):
    with open(name, "a", encoding="utf-8") as handle:
        handle.write(text)
    subprocess.run(["git", "add", name], check=True)
    subprocess.run(
        ["git", "-c", "user.name=" + who, "-c", "user.email=f@example.invalid",
         "commit", "-m", who + ": change"],
        check=True, capture_output=True,
    )


def main():
    argv = sys.argv[1:]
    interactive = "-p" not in argv
    prompt = "" if interactive else sys.stdin.read()
    with open(os.environ["JOSI_FAKE_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"argv": argv, "cwd": os.getcwd(), "stdin": prompt}
        ) + "\\n")
    if interactive:
        return 0
    if "--disallowedTools" in argv:
        text = os.environ.get(
            "JOSI_FAKE_CLAUDE_REVIEW_JSON",
            '{"decision":"approved","findings":[]}',
        )
    elif prompt.startswith("Write the tests for this task"):
        text = '{"decision":"no_tests_needed","reason":"none"}'
    else:
        commit("worker-output.txt", "worker change\\n", "Fake Worker")
        text = "done"
    session = argv[argv.index("--session-id") + 1] if "--session-id" in argv \\
        else argv[argv.index("--resume") + 1]
    print(json.dumps({"type": "system", "subtype": "init",
                      "session_id": session}))
    print(json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "thinking aloud"}]}}))
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": text, "session_id": session, "total_cost_usd": 0.05,
        "usage": {
            "input_tokens": 10, "output_tokens": 20,
            "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40,
        },
    }))
    return int(os.environ.get("JOSI_FAKE_CLAUDE_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
'''


class ClaudeTestCase(JosiTestCase):
    """A repository whose fake ``claude`` executable is on ``PATH``."""

    def setUp(self):
        super().setUp()
        fake = self.bin / "claude"
        fake.write_text(
            FAKE_CLAUDE.replace("#!python", "#!" + sys.executable, 1),
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=str(self.repo), env=self.env,
            capture_output=True, text=True, check=True,
        )

    def commit_base(self):
        self.init_git()
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        (self.repo / ".gitignore").write_text(".josi/\n/josi.json\n", encoding="utf-8")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "-A")
        self.git(
            "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
            "commit", "-m", "base commit",
        )

    def metadata(self):
        return json.loads(self.metadata_path().read_text(encoding="utf-8"))

    def claude_calls(self):
        return self.calls()

    def flag(self, call, name):
        argv = call["argv"]
        return argv[argv.index(name) + 1]


@unittest.skipUnless(shutil.which("git"), "git is required")
class ClaudeInitTests(ClaudeTestCase):
    def test_init_stores_claude_defaults_as_pairs(self):
        self.commit_base()
        result = self.run_josi("init", "--agent", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = self.metadata()
        self.assertEqual(metadata["agent"], "claude")
        self.assertEqual(
            metadata["worker_model"], {"model": "sonnet", "effort": "low"}
        )
        self.assertEqual(
            metadata["reviewer_model"], {"model": "opus", "effort": "medium"}
        )
        self.assertEqual(
            metadata["escalation_worker_model"],
            {"model": "opus", "effort": "medium"},
        )
        self.assertEqual(
            metadata["test_writer_model"],
            {"model": "sonnet", "effort": "medium"},
        )
        self.assertIn("Agent: claude", result.stdout)
        self.assertIn("Worker model: sonnet#low", result.stdout)

    def test_default_agent_is_opencode_with_pair_models(self):
        self.commit_base()
        self.assertEqual(self.run_josi("init").returncode, 0)
        metadata = self.metadata()
        self.assertEqual(metadata["agent"], "opencode")
        self.assertEqual(
            metadata["worker_model"],
            {"model": "openrouter/deepseek/deepseek-v4.1-flash",
             "effort": "low"},
        )

    def test_explicit_models_override_the_claude_defaults(self):
        self.commit_base()
        result = self.run_josi(
            "init", "--agent", "claude", "--worker-model", "opus#high",
            "--reviewer-model", "sonnet",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = self.metadata()
        self.assertEqual(
            metadata["worker_model"], {"model": "opus", "effort": "high"}
        )
        self.assertEqual(
            metadata["reviewer_model"], {"model": "sonnet", "effort": None}
        )

    def test_invalid_claude_effort_is_refused(self):
        self.commit_base()
        result = self.run_josi(
            "init", "--agent", "claude", "--worker-model", "sonnet#turbo"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid worker model", result.stderr)
        self.assertFalse(self.metadata_path().exists())

    def test_provider_model_names_are_not_required_for_claude(self):
        self.commit_base()
        result = self.run_josi(
            "init", "--agent", "claude", "--worker-model", "claude-sonnet-5-5"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_switching_agents_resets_models_to_the_new_defaults(self):
        self.commit_base()
        self.assertEqual(
            self.run_josi("init", "--worker-model", "custom/model#x").returncode,
            0,
        )
        result = self.run_josi("init", "--agent", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the agent to claude", result.stdout)
        metadata = self.metadata()
        self.assertEqual(metadata["agent"], "claude")
        self.assertEqual(
            metadata["worker_model"], {"model": "sonnet", "effort": "low"}
        )

    def test_switching_agents_keeps_explicit_models(self):
        self.commit_base()
        self.assertEqual(self.run_josi("init").returncode, 0)
        result = self.run_josi(
            "init", "--agent", "claude", "--reviewer-model", "opus#high"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = self.metadata()
        self.assertEqual(
            metadata["reviewer_model"], {"model": "opus", "effort": "high"}
        )
        self.assertEqual(
            metadata["worker_model"], {"model": "sonnet", "effort": "low"}
        )

    def test_reinit_without_agent_preserves_the_stored_agent_and_models(self):
        self.commit_base()
        self.assertEqual(
            self.run_josi(
                "init", "--agent", "claude", "--worker-model", "opus#max"
            ).returncode,
            0,
        )
        result = self.run_josi("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already initialized", result.stdout)
        metadata = self.metadata()
        self.assertEqual(metadata["agent"], "claude")
        self.assertEqual(
            metadata["worker_model"], {"model": "opus", "effort": "max"}
        )

    def test_legacy_string_models_are_read_transparently(self):
        self.commit_base()
        self.assertEqual(self.run_josi("init").returncode, 0)
        metadata = self.metadata()
        del metadata["agent"]
        metadata["worker_model"] = "provider/model#low"
        self.metadata_path().write_text(json.dumps(metadata), encoding="utf-8")
        result = self.run_josi("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already initialized", result.stdout)
        self.assertEqual(self.metadata()["worker_model"], "provider/model#low")

    def test_missing_claude_executable_fails_init(self):
        self.commit_base()
        (self.bin / "claude").unlink()
        self.env["PATH"] = str(self.bin) + ":/usr/bin:/bin"
        result = self.run_josi("init", "--agent", "claude")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Claude Code is not installed", result.stderr)


@unittest.skipUnless(shutil.which("git"), "git is required")
class ClaudePlanTests(ClaudeTestCase):
    def setUp(self):
        super().setUp()
        self.commit_base()
        self.assertEqual(
            self.run_josi("init", "--agent", "claude").returncode, 0
        )

    def test_plan_launches_an_interactive_restricted_claude(self):
        result = self.run_josi("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        (call,) = self.claude_calls()
        argv = call["argv"]
        self.assertNotIn("-p", argv)
        self.assertEqual(call["cwd"], str(self.repo))
        self.assertNotIn("--model", argv)
        prompt = self.flag(call, "--append-system-prompt")
        self.assertIn("planning assistant", prompt)
        self.assertNotIn("description:", prompt)
        self.assertIn("--strict-mcp-config", argv)
        denied = self.flag(call, "--disallowedTools").split(",")
        for tool in ("Edit", "Write", "WebFetch", "Bash(josi task run:*)",
                     "Bash(josi run:*)", "Bash(josi task reset:*)"):
            self.assertIn(tool, denied)
        allowed = self.flag(call, "--allowedTools").split(",")
        self.assertIn("Bash(josi task create:*)", allowed)
        self.assertNotIn("Bash(josi task run:*)", allowed)

    def test_every_plan_run_starts_a_fresh_session(self):
        self.assertEqual(self.run_josi("plan").returncode, 0)
        self.assertEqual(self.run_josi("plan").returncode, 0)
        first, second = self.claude_calls()
        self.assertNotEqual(
            self.flag(first, "--session-id"), self.flag(second, "--session-id")
        )
        self.assertFalse(self.planner_path().exists())

    def test_plan_does_not_install_the_opencode_agent_file(self):
        self.assertEqual(self.run_josi("plan").returncode, 0)
        self.assertFalse(self.agent_path().exists())

    def test_plan_propagates_the_exit_status(self):
        (self.bin / "claude").write_text(
            "#!/bin/sh\nexit 3\n", encoding="utf-8"
        )
        result = self.run_josi("plan")
        self.assertEqual(result.returncode, 3)
        self.assertIn("Claude Code exited with status 3", result.stderr)


@unittest.skipUnless(shutil.which("git"), "git is required")
class ClaudeRunTests(ClaudeTestCase):
    def setUp(self):
        super().setUp()
        self.commit_base()
        self.assertEqual(
            self.run_josi("init", "--agent", "claude").returncode, 0
        )
        result = self.run_josi(
            "task", "create", "--title", "Add the feature",
            "--description", "Do the thing.",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_task_run_drives_claude_for_every_role(self):
        result = self.run_josi("task", "run", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        tests, worker, reviewer = self.claude_calls()

        for call in (tests, worker, reviewer):
            argv = call["argv"]
            self.assertEqual(argv[0], "-p")
            self.assertEqual(
                self.flag(call, "--permission-mode"), "bypassPermissions"
            )
            self.assertEqual(self.flag(call, "--output-format"), "stream-json")
            self.assertIn("--verbose", argv)
            # The prompt travels on stdin, never in the argument vector.
            self.assertTrue(call["stdin"].strip())
            self.assertNotIn(call["stdin"], argv)

        self.assertEqual(self.flag(tests, "--model"), "sonnet")
        self.assertEqual(self.flag(tests, "--effort"), "medium")
        self.assertEqual(self.flag(worker, "--model"), "sonnet")
        self.assertEqual(self.flag(worker, "--effort"), "low")
        self.assertEqual(self.flag(reviewer, "--model"), "opus")
        self.assertEqual(self.flag(reviewer, "--effort"), "medium")
        self.assertIn("Edit", self.flag(reviewer, "--disallowedTools"))
        self.assertNotIn("--disallowedTools", worker["argv"])
        self.assertTrue(self.worktree_path().is_dir())
        self.assertEqual(worker["cwd"], str(self.worktree_path()))
        self.assertNotEqual(reviewer["cwd"], worker["cwd"])
        self.assertIn("worker-output.txt", self.git(
            "ls-tree", "-r", "--name-only", "josi/t1"
        ).stdout)

    def worktree_path(self):
        return self.repo / ".josi" / "worktrees" / "t1"

    def test_each_session_starts_once_with_session_id(self):
        self.assertEqual(self.run_josi("task", "run", "t1").returncode, 0)
        sessions = [self.flag(c, "--session-id") for c in self.claude_calls()]
        self.assertEqual(len(set(sessions)), 3)

    def test_reviewer_format_retry_resumes_the_same_session(self):
        self.env["JOSI_FAKE_CLAUDE_REVIEW_JSON"] = "not json at all"
        result = self.run_josi("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        reviewer, retry = self.claude_calls()[-2:]
        self.assertIn("--session-id", reviewer["argv"])
        self.assertEqual(
            self.flag(retry, "--resume"), self.flag(reviewer, "--session-id")
        )
        self.assertNotIn("--session-id", retry["argv"])

    def test_usage_is_recorded_from_the_result_events(self):
        self.assertEqual(self.run_josi("task", "run", "t1").returncode, 0)
        shown = self.run_josi("task", "show", "t1")
        self.assertIn("cost_total: 0.150000", shown.stdout)
        logs = sorted((self.repo / ".josi" / "runs").glob("t1-*.log"))
        text = "\n".join(path.read_text(encoding="utf-8") for path in logs)
        self.assertIn(
            "usage: input=10 output=20 reasoning=unavailable"
            " cache_read=30 cache_write=40",
            text,
        )

    def test_a_failing_worker_fails_the_task(self):
        self.env["JOSI_FAKE_CLAUDE_EXIT"] = "1"
        result = self.run_josi("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Claude Code", result.stdout + result.stderr)

    def test_missing_claude_executable_refuses_the_run(self):
        (self.bin / "claude").unlink()
        self.env["PATH"] = str(self.bin) + ":/usr/bin:/bin"
        result = self.run_josi("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Claude Code is not installed", result.stderr)


class AgentsModuleTests(unittest.TestCase):
    def test_parse_model_for_opencode(self):
        self.assertEqual(
            agents.parse_model("opencode", " a/b#low ", "worker"), "a/b#low"
        )
        with self.assertRaises(ValueError):
            agents.parse_model("opencode", "sonnet", "worker")

    def test_parse_model_for_claude(self):
        self.assertEqual(
            agents.parse_model("claude", "sonnet#xhigh", "worker"),
            "sonnet#xhigh",
        )
        for bad in ("", "-x", "so net", "sonnet#", "sonnet#fast"):
            with self.assertRaises(ValueError, msg=bad):
                agents.parse_model("claude", bad, "worker")

    def test_model_pair_round_trip(self):
        self.assertEqual(agents.split_model("opus#high"), ("opus", "high"))
        self.assertEqual(agents.split_model("opus"), ("opus", ""))
        self.assertEqual(agents.join_model("opus", None), "opus")
        self.assertEqual(agents.join_model("opus", "high"), "opus#high")

    def test_final_text_uses_the_last_result_event(self):
        backend = agents.ClaudeBackend("claude")
        output = "\n".join([
            "stderr noise",
            json.dumps({"type": "result", "result": "first"}),
            json.dumps({"type": "result", "result": "second"}),
        ])
        self.assertEqual(backend.final_text(output), "second")
        self.assertEqual(backend.final_text("no events"), "")
        error = json.dumps(
            {"type": "result", "result": "boom", "is_error": True}
        )
        self.assertEqual(backend.final_text(error), "")

    def test_turn_usage_reads_the_result_event(self):
        backend = agents.ClaudeBackend("claude")
        output = json.dumps({
            "type": "result", "total_cost_usd": 0.5,
            "usage": {"input_tokens": 1, "output_tokens": 2,
                      "cache_read_input_tokens": 3,
                      "cache_creation_input_tokens": 4},
        })
        cumulative, record = backend.turn_usage(None, None, "sonnet", output, None)
        self.assertIsNone(cumulative)
        self.assertTrue(record.available)
        self.assertEqual(
            (record.input_tokens, record.output_tokens,
             record.cache_read_tokens, record.cache_write_tokens),
            (1, 2, 3, 4),
        )
        self.assertEqual(record.cost_total, 0.5)
        self.assertEqual(record.cost_source, usage.COST_REPORTED)

    def test_turn_usage_without_a_result_is_unavailable(self):
        backend = agents.ClaudeBackend("claude")
        _, record = backend.turn_usage(None, None, "sonnet", "nothing", None)
        self.assertFalse(record.available)


if __name__ == "__main__":
    unittest.main()
