"""Codex integration tests use a fake executable, never model calls."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from test_claude import ClaudeTestCase, FAKE_CLAUDE
from josi import agents

# Keep the same Git behavior as the Claude fixture; replace its CLI protocol.
FAKE_CODEX = FAKE_CLAUDE.replace('interactive = "-p" not in argv', 'interactive = "exec" not in argv').replace(
    'if "--disallowedTools" in argv:', 'if "read-only" in argv:'
).replace(
    'session = argv[argv.index("--session-id") + 1] if "--session-id" in argv \\\n        else argv[argv.index("--resume") + 1]',
    'session = argv[argv.index("resume") + 1] if "resume" in argv else __import__("uuid").uuid4().hex'
)
FAKE_CODEX = FAKE_CODEX.replace(
    'elif prompt.startswith("Write the tests for this task"):',
    'elif prompt.startswith("Write the tests for this task") or ("resume" in argv and "read-only" not in argv):',
)
start = FAKE_CODEX.index('    print(json.dumps({"type": "system"')
end = FAKE_CODEX.index('    return int(', start)
FAKE_CODEX = FAKE_CODEX[:start] + '''    if not os.environ.get("JOSI_FAKE_NO_THREAD"):
        print(json.dumps({"type":"thread.started", "thread_id":session}))
    print(json.dumps({"type":"turn.started"}))
    if os.environ.get("JOSI_FAKE_EVENTS") and (
        not os.environ.get("JOSI_FAKE_WORKER_ONLY") or
        ("read-only" not in argv and not prompt.startswith("Write the tests for this task"))
    ):
        print(os.environ["JOSI_FAKE_EVENTS"])
        return 0
    if os.environ.get("JOSI_FAKE_TAMPER") and "read-only" in argv:
        open("README.md", "w").write("changed")
    if prompt.startswith("Write the tests for this task") or ("resume" in argv and "read-only" not in argv):
        text = os.environ.get("JOSI_FAKE_TEST_JSON", text)
    print(json.dumps({"type":"item.completed", "item":{
        "type":"agent_message", "text":text}}))
    print(json.dumps({"type":"turn.completed", "usage":{
        "input_tokens":40, "cached_input_tokens":30, "output_tokens":20}}))
''' + FAKE_CODEX[end:]


class CodexTestCase(ClaudeTestCase):
    def setUp(self):
        super().setUp()
        fake = self.bin / "codex"
        fake.write_text(FAKE_CODEX.replace("#!python", "#!" + sys.executable, 1))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)


class CodexInitTests(CodexTestCase):
    def test_defaults_overrides_and_switching(self):
        self.commit_base()
        result = self.run_josi("init", "--agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        for key in agents.MODEL_KEYS:
            name, effort = agents.DEFAULT_MODELS["codex"][key]
            self.assertEqual(self.metadata()[key], {"model":name, "effort":effort})
        for key in agents.MODEL_KEYS:
            result = self.run_josi("init", "--" + key.replace("_", "-"), "custom#xhigh")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_josi("init", "--agent", "codex").returncode, 0)
        self.assertTrue(all(self.metadata()[k]["model"] == "custom" for k in agents.MODEL_KEYS))
        self.assertEqual(self.run_josi("init", "--agent", "claude").returncode, 0)
        self.assertEqual(self.metadata()["worker_model"]["model"], "sonnet")
        self.assertEqual(self.run_josi("init", "--agent", "codex", "--worker-model", "other#none").returncode, 0)
        self.assertEqual(self.metadata()["worker_model"]["model"], "other")
        self.assertEqual(self.metadata()["reviewer_model"]["model"], "gpt-6.1-sol")

    def test_missing_executable(self):
        self.commit_base()
        (self.bin / "codex").unlink()
        self.env["PATH"] = str(self.bin) + ":/usr/bin:/bin"
        result = self.run_josi("init", "--agent", "codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Codex is not installed", result.stderr)

    def test_effort_validation(self):
        for effort in agents.CODEX_EFFORTS:
            self.assertEqual(agents.parse_model("codex", "model#" + effort, "worker"), "model#" + effort)
        for bad in ("", "-model", "model#", "model#max", "model#turbo", "a b"):
            with self.assertRaises(ValueError):
                agents.parse_model("codex", bad, "worker")


class CodexRunTests(CodexTestCase):
    def setUp(self):
        super().setUp()
        self.commit_base()
        result = self.run_josi("init", "--agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_josi("task", "create", "--title", "Feature", "--description", "Implement it").returncode, 0)

    def test_all_roles_and_usage(self):
        result = self.run_josi("task", "run", "t1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tests, worker, reviewer = self.calls()
        for call in (tests, worker, reviewer):
            self.assertEqual(call["argv"][:2], ["exec", "--json"])
            self.assertTrue(call["stdin"])
            self.assertEqual(call["argv"][-1], "-")
            self.assertEqual(self.flag(call, "--model"), "gpt-6.1-sol")
        for call in (tests, worker):
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", call["argv"])
        self.assertIn('model_reasoning_effort="low"', worker["argv"])
        self.assertEqual(worker["cwd"], str(self.repo / ".josi/worktrees/t1"))
        self.assertEqual(self.flag(reviewer, "--sandbox"), "read-only")
        self.assertIn('approval_policy="never"', reviewer["argv"])
        logs = "\n".join(p.read_text() for p in (self.repo / ".josi/runs").glob("t1-*.log"))
        self.assertIn("codex_thread:", logs)
        self.assertIn("usage: input=10 output=20 reasoning=unavailable cache_read=30 cache_write=unavailable", logs)
        self.assertIn("cost_total: unavailable", self.run_josi("task", "show", "t1").stdout)

    def test_reviewer_retry_preserves_permissions_and_thread(self):
        self.env["JOSI_FAKE_CLAUDE_REVIEW_JSON"] = "invalid"
        result = self.run_josi("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        first, retry = self.calls()[-2:]
        self.assertNotIn("resume", first["argv"])
        self.assertIn("resume", retry["argv"])
        thread = retry["argv"][retry["argv"].index("resume") + 1]
        logs = "\n".join(p.read_text() for p in (self.repo / ".josi/runs").glob("t1-*.log"))
        self.assertIn("codex_thread: " + thread, logs)
        self.assertEqual(first["argv"][:-1], retry["argv"][:-3])

    def test_test_writer_format_retry(self):
        self.env["JOSI_FAKE_TEST_JSON"] = "invalid"
        self.assertNotEqual(self.run_josi("task", "run", "t1").returncode, 0)
        first, retry = self.calls()
        self.assertIn("resume", retry["argv"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", retry["argv"])
        self.assertEqual(self.git("rev-parse", "josi/t1").stdout, self.git("rev-parse", "main").stdout)

    def test_missing_thread_cannot_resume(self):
        self.env["JOSI_FAKE_NO_THREAD"] = "1"
        self.env["JOSI_FAKE_CLAUDE_REVIEW_JSON"] = "invalid"
        result = self.run_josi("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no thread.started", result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 3)

    def test_escalation(self):
        self.env["JOSI_FAKE_CLAUDE_REVIEW_JSON"] = json.dumps({"decision":"changes_requested", "findings":[{"severity":"major", "description":"Broken", "scenario":"Normal usage", "impact":"Fails", "location":"worker-output.txt"}]})
        self.assertNotEqual(self.run_josi("task", "run", "t1").returncode, 0)
        # Worker prompts include attempt-specific feedback; use model flags.
        self.assertIn("gpt-6-astra", [self.flag(c, "--model") for c in self.calls()])

    def test_checkout_integrity(self):
        self.env["JOSI_FAKE_TAMPER"] = "1"
        result = self.run_josi("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("review", result.stdout + result.stderr)

    def test_process_failure(self):
        self.env["JOSI_FAKE_CLAUDE_EXIT"] = "7"
        self.assertNotEqual(self.run_josi("task", "run", "t1").returncode, 0)

    def test_incomplete_stream(self):
        self.env["JOSI_FAKE_EVENTS"] = "broken"
        result = self.run_josi("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("no text output", result.stdout + result.stderr)

    def test_incomplete_worker_cannot_succeed_after_committing(self):
        self.env["JOSI_FAKE_EVENTS"] = "broken"
        self.env["JOSI_FAKE_WORKER_ONLY"] = "1"
        result = self.run_josi("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incomplete event stream", result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 2)

    def test_failed_event_with_zero_exit_status(self):
        self.env["JOSI_FAKE_EVENTS"] = '{"type":"turn.failed","error":{"message":"offline"}}'
        result = self.run_josi("task", "run", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Codex turn failed", result.stdout + result.stderr)
        self.assertIn("offline", result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_planner_process_failure(self):
        fake = self.bin / "codex"
        fake.write_text("#!" + sys.executable + "\nimport sys\nsys.exit(9)\n")
        result = self.run_josi("plan")
        self.assertEqual(result.returncode, 9)
        self.assertIn("Codex exited with status 9", result.stderr)

    def test_planner(self):
        for _ in range(2):
            self.assertEqual(self.run_josi("plan").returncode, 0)
        for call in self.calls():
            self.assertEqual(call["cwd"], str(self.repo))
            self.assertEqual(call["stdin"], "")
            self.assertNotIn("--model", call["argv"])
            self.assertEqual(self.flag(call, "--sandbox"), "read-only")
            self.assertEqual(self.flag(call, "--ask-for-approval"), "on-request")
            self.assertIn('web_search="disabled"', call["argv"])
            self.assertIn("features.multi_agent=false", call["argv"])
            self.assertTrue(any(v.startswith("developer_instructions=") for v in call["argv"]))
            instructions = next(v for v in call["argv"] if v.startswith("developer_instructions="))
            self.assertIn("read-only tools and shell commands without asking", instructions)
            self.assertNotIn("Every other shell command asks", instructions)
            self.assertNotIn("request approval for task-management writes", instructions)
            self.assertIn("Create, update, or delete tasks only when the user asks", instructions)
        self.assertEqual(
            (self.repo / ".codex/rules/josi-planner.rules").read_text(),
            agents.CODEX_PLANNER_RULES,
        )
        self.assertFalse((self.repo / "AGENTS.md").exists())
        self.assertFalse((self.repo / ".opencode/agents/josi-planner.md").exists())

    def test_planner_preserves_custom_configuration_and_refreshes_managed_rules(self):
        config_dir = self.repo / ".codex"
        rules_dir = config_dir / "rules"
        rules_dir.mkdir(parents=True)
        config = config_dir / "config.toml"
        config.write_text('model = "custom-model"\n')
        custom_rules = rules_dir / "custom.rules"
        custom_rules.write_text('# User rules\n')
        managed_rules = rules_dir / "josi-planner.rules"
        managed_rules.write_text('# Outdated managed rules\n')
        result = self.run_josi("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_text(), 'model = "custom-model"\n')
        self.assertEqual(custom_rules.read_text(), '# User rules\n')
        self.assertEqual(managed_rules.read_text(), agents.CODEX_PLANNER_RULES)
        self.assertIn("Codex must trust this project", result.stdout)
        result = self.run_josi("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Installed Josi task rules", result.stdout)

    def test_planner_rule_install_failure_does_not_launch_codex(self):
        (self.repo / ".codex").write_text("not a directory")
        result = self.run_josi("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not launch Codex", result.stderr)
        self.assertEqual(self.calls(), [])


class CodexEventTests(unittest.TestCase):
    def setUp(self):
        self.backend = agents.CodexBackend("missing-codex")

    def stream(self, *events):
        return "malformed\n[]\n" + "\n".join(json.dumps(e) for e in events)

    def test_final_completed_message_only(self):
        message = lambda text: {"type":"item.completed", "item":{"type":"agent_message", "text":text}}
        completed = {"type":"turn.completed", "usage":{"input_tokens":40, "cached_input_tokens":30, "output_tokens":20}}
        output = self.stream({"type":"turn.started"}, message("earlier"), message("final"), completed)
        self.assertEqual(self.backend.final_text(output), "final")
        for ending in ({"type":"turn.started"}, {"type":"turn.failed"}, {"type":"error"}, message("later incomplete"), {"type":"turn.completed"}):
            self.assertEqual(self.backend.final_text(output + "\n" + json.dumps(ending)), "")
        self.assertEqual(self.backend.final_text(self.stream(message("incomplete"))), "")
        _, record = self.backend.turn_usage(None, None, None, output, None)
        self.assertEqual((record.input_tokens, record.cache_read_tokens, record.output_tokens), (10, 30, 20))
        self.assertIsNone(record.cost_total)

    def test_missing_usage_and_subprocess_error(self):
        _, record = self.backend.turn_usage(None, None, None, "broken", None)
        self.assertFalse(record.available)
        with tempfile.TemporaryDirectory() as directory:
            session = self.backend.create_session(Path(directory), "title", "worker")
            with open(Path(directory) / "log", "w") as log:
                with self.assertRaisesRegex(agents.AgentError, "could not run Codex"):
                    self.backend.run_headless(directory, session, "model#low", "prompt", log, "worker")

    def test_malformed_usage_counters_remain_unavailable(self):
        for value in (True, -1, "10", float("inf"), None, [], {}):
            output = self.stream({"type":"turn.completed", "usage":{
                "input_tokens":value, "cached_input_tokens":value,
                "output_tokens":value,
            }})
            _, record = self.backend.turn_usage(None, None, None, output, None)
            self.assertIsNone(record.input_tokens)
            self.assertIsNone(record.output_tokens)
            self.assertIsNone(record.cache_read_tokens)

    def test_execution_lock_descriptor_is_inherited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            read_fd, write_fd = os.pipe()
            try:
                fake = root / "codex"
                fake.write_text(
                    "#!" + sys.executable + "\nimport os, sys\n"
                    "sys.stdin.read()\n"
                    + "os.write(" + str(write_fd) + ", b'held')\n"
                    + "print('{\"type\":\"turn.completed\"}')\n"
                )
                fake.chmod(0o700)
                backend = agents.CodexBackend(str(fake))
                session = backend.create_session(root, "title", "worker")
                with open(root / "log", "w") as log:
                    result = backend.run_headless(
                        root, session, "model#low", "prompt", log, "worker",
                        pass_fds=(write_fd,),
                    )
                self.assertEqual(result, 0)
                self.assertEqual(os.read(read_fd, 4), b"held")
            finally:
                os.close(read_fd)
                os.close(write_fd)
