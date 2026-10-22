"""End-to-end tests for actl Stage 3: planner integration with task management.

Everything runs through the fake ``opencode`` executable from the Stage 1
tests, so the suite needs no real OpenCode process and no model calls. The
planner agent's permission rules are evaluated with a matcher that mirrors
the installed OpenCode 2.1.2 exactly (verified against its permission
engine): a rule is ``{action, resource, effect}`` where ``effect`` is one of
allow/deny/ask, both action and resource are matched as glob patterns (only
``*`` and ``?`` are special, matching is case-sensitive), the last matching
rule wins, and a request that matches no rule defaults to ask. The agent file
opens with a catch-all deny, so evaluating its rules alone yields the same
decision as OpenCode's full base-plus-agent ruleset.
"""

from __future__ import annotations

import re
import shlex
import shutil
import sqlite3
import unittest

from test_actl import ActlTestCase


def opencode_glob_match(value: str, pattern: str) -> bool:
    """Reimplementation of OpenCode 2.1.2's permission glob match."""
    value = value.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    pattern = re.sub(r"([.+^${}()|\[\]\\])", r"\\\1", pattern)
    pattern = pattern.replace("*", ".*").replace("?", ".")
    if pattern.endswith(" .*"):
        # A pattern like "git diff *" also matches the bare "git diff".
        pattern = pattern[:-3] + "( .*)?"
    return re.fullmatch(pattern, value, re.S) is not None


class Ruleset:
    """Permission rules evaluated the way OpenCode evaluates them."""

    def __init__(self, rules):
        self.rules = rules

    def effect(self, action: str, resource: str) -> str:
        for rule in reversed(self.rules):
            if (
                opencode_glob_match(action, rule["action"])
                and opencode_glob_match(resource, rule["resource"])
            ):
                return rule["effect"]
        return "ask"


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlannerRulesTests(ActlTestCase):
    """The installed agent pre-approves exactly the actl task commands."""

    def setUp(self):
        super().setUp()
        self.initialize()
        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.ruleset = Ruleset(self.planner_rules())

    def test_task_commands_are_preapproved(self):
        commands = [
            "actl task list",
            "actl task show t1",
            "actl task show 3",
            "actl task create --title 'Add login'",
            'actl task create --title "Add login" --description "Form with validation"',
            "actl task create --title Tests --description-file acceptance.md",
            "actl task create --title Tests --depends-on t1",
            "actl task create --title Tests --depends-on t1,t2",
            "actl task update t2 --title 'New title'",
            "actl task update t2 --description 'New body'",
            "actl task update t2 --description-file updated.md",
            "actl task update t2 --depends-on t1",
            "actl task update t2 --depends-on t1 --depends-on t3",
            "actl task update t2 --clear-dependencies",
            "actl task delete t2",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.ruleset.effect("shell", command), "allow")

    def test_unrelated_shell_commands_still_ask(self):
        commands = [
            "git status",
            "git log --oneline -5",
            "cat README.md",
            "head -5 README.md",
            "grep -n TODO src/actl/cli.py",
            "ls -la src",
            "rm -rf src",
            "mv src/x src/y",
            "cp src/x src/y",
            "sed -i s/a/b/ src/main.py",
            "git diff --output=file",
            "git commit -m 'x'",
            "python -c 'print(1)'",
            "actl init",
            "actl plan",
            "actlfake task list",
            "actl taskrun",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.ruleset.effect("shell", command), "ask")

    def test_actl_task_run_is_denied(self):
        commands = [
            "actl task run",
            "actl task run t1",
            "actl task run 3",
            "actl task run t2 --worker-model provider/model",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.ruleset.effect("shell", command), "deny")

    def test_task_management_survives_the_run_deny(self):
        commands = [
            "actl task list",
            "actl task show t1",
            "actl task create --title 'Add login'",
            "actl task update t1 --title 'New title'",
            "actl task delete t1",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.ruleset.effect("shell", command), "allow")
        # Only the real subcommand is blocked.
        self.assertEqual(self.ruleset.effect("shell", "actl taskrun"), "ask")
        self.assertEqual(self.ruleset.effect("shell", "actl tasks run t1"), "ask")

    def test_task_commands_cannot_redirect_output_to_files(self):
        # OpenCode reports a redirected statement as a single resource string,
        # so "actl task ... > file" would match the broad "actl task *" allow.
        # The redirect deny is placed after the pre-approval (last match wins)
        # so a silently allowed command can never write or overwrite a file.
        commands = [
            "actl task list > src/main.py",
            "actl task list >> out.txt",
            "actl task list 2>&1",
            "actl task show t1 > report.txt",
            "actl task create --title x > result.txt",
            "echo hi > src/main.py",
            "git status > log.txt",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(self.ruleset.effect("shell", command), "deny")

    def test_task_allow_stops_at_the_command_boundary(self):
        # Only the literal "actl task" prefix is pre-approved; lookalikes ask.
        self.assertEqual(self.ruleset.effect("shell", "actl task"), "allow")
        self.assertEqual(self.ruleset.effect("shell", "actl tasks list"), "ask")
        self.assertEqual(self.ruleset.effect("shell", "actl-task list"), "ask")
        self.assertEqual(self.ruleset.effect("shell", "acctl task list"), "ask")

    def test_read_only_inspection_and_questions_are_preserved(self):
        self.assertEqual(self.ruleset.effect("read", "src/actl/cli.py"), "allow")
        self.assertEqual(self.ruleset.effect("read", ".env"), "ask")
        self.assertEqual(self.ruleset.effect("read", ".env.local"), "ask")
        self.assertEqual(self.ruleset.effect("read", ".env.example"), "allow")
        self.assertEqual(self.ruleset.effect("glob", "src/**/*.py"), "allow")
        self.assertEqual(self.ruleset.effect("grep", "TODO"), "allow")
        self.assertEqual(self.ruleset.effect("question", "*"), "allow")

    def test_editing_and_unknown_tools_are_denied(self):
        cases = [
            ("edit", "src/actl/cli.py"),
            ("write", "src/actl/cli.py"),
            ("patch", "src/actl/cli.py"),
            ("subagent", "general"),
            ("webfetch", "https://example.com"),
            ("websearch", "actl documentation"),
            ("todowrite", "*"),
            ("skill", "*"),
            ("execute", "*"),
            # MCP tools use "server:tool" as the action; nothing allows them.
            ("filesystem:write_file", "*"),
            ("github:create_issue", "*"),
            ("opencode_read_mcp_resource", "github:*"),
        ]
        for action, resource in cases:
            with self.subTest(action=action):
                self.assertEqual(self.ruleset.effect(action, resource), "deny")


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlannerInstructionsTests(ActlTestCase):
    """The planner instructions document the real actl task CLI."""

    def setUp(self):
        super().setUp()
        self.initialize()
        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.content = self.agent_path().read_text(encoding="utf-8")

    def test_documents_the_actual_task_subcommands(self):
        for subcommand in ("create", "list", "show", "update", "delete"):
            with self.subTest(subcommand=subcommand):
                # The documented command exists and has this exact syntax.
                help_result = self.run_actl("task", subcommand, "--help")
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                self.assertIn("usage: actl task " + subcommand, help_result.stdout)
                self.assertIn("actl task " + subcommand, self.content)

    def test_documents_the_actual_task_options(self):
        for option in (
            "--title",
            "--description",
            "--description-file",
            "--depends-on",
            "--clear-dependencies",
        ):
            with self.subTest(option=option):
                self.assertIn(option, self.content)
        # Dependencies, ids, and persistence rules are spelled out.
        self.assertIn("t1", self.content)
        self.assertIn("comma-separated", self.content)
        self.assertIn("cycle", self.content)

    def test_documents_task_writing_guidance_and_task_rules(self):
        for phrase in (
            "acceptance criteria",
            "description",
            "depends",
            "never open .actl/tasks.db",
            "Do not modify project code",
            "Do not execute implementation tasks",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.content)

    def test_instructions_are_installed_as_the_system_prompt(self):
        # The body (not just the frontmatter) is part of the agent file.
        self.assertTrue(self.content.startswith("---\n"))
        self.assertIn("You are the planning assistant for this repository.", self.content)


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlannerTaskWorkflowTests(ActlTestCase):
    """Task management through the commands the planner is allowed to run."""

    def setUp(self):
        super().setUp()
        self.initialize()
        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.ruleset = Ruleset(self.planner_rules())
        self.src = self.repo / "src"
        self.src.mkdir()
        (self.src / "main.py").write_text("print('hello')\n", encoding="utf-8")
        (self.repo / "acceptance.md").write_text(
            "Run `actl task list` and check the new task appears.\n", encoding="utf-8"
        )

    def task_db(self):
        return self.repo / ".actl" / "tasks.db"

    def planner_command(self, *args):
        """Run a task command exactly as the planner would: only if allowed."""
        command = shlex.join(["actl", *args])
        self.assertEqual(
            self.ruleset.effect("shell", command),
            "allow",
            "the planner may not run: " + command,
        )
        return self.run_actl(*args)

    def test_workflow_creates_updates_deletes_and_persists_tasks(self):
        source_before = (self.src / "main.py").read_bytes()

        created = self.planner_command(
            "task", "create", "--title", "Define the task schema",
            "--description", "Add the tasks and dependencies tables.",
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn("Created task t1", created.stdout)

        created = self.planner_command(
            "task", "create", "--title", "Write acceptance criteria",
            "--description-file", "acceptance.md",
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn("Created task t2", created.stdout)

        created = self.planner_command(
            "task", "create", "--title", "Wire the planner",
            "--depends-on", "t1", "--depends-on", "t2",
        )
        self.assertEqual(created.returncode, 0, created.stderr)

        listing = self.planner_command("task", "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(
            [line.split("  ")[0] for line in listing.stdout.splitlines()],
            ["t1", "t2", "t3"],
        )

        shown = self.planner_command("task", "show", "t3")
        self.assertIn("title: Wire the planner", shown.stdout)
        self.assertIn("status: PENDING", shown.stdout)
        self.assertIn("depends_on: t1, t2", shown.stdout)

        # Deleting a task that others depend on is refused...
        refused = self.planner_command("task", "delete", "t1")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("cannot delete task t1", refused.stderr)

        # ...until the dependent is updated.
        updated = self.planner_command(
            "task", "update", "t3", "--clear-dependencies"
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertIn("depends_on: (none)", self.planner_command("task", "show", "t3").stdout)

        updated = self.planner_command(
            "task", "update", "t3", "--title", "Wire the planner to tasks",
            "--description", "Resume the session and manage tasks.",
            "--depends-on", "t2",
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        shown = self.planner_command("task", "show", "t3")
        self.assertIn("title: Wire the planner to tasks", shown.stdout)
        self.assertIn("description:\nResume the session and manage tasks.", shown.stdout)
        self.assertIn("depends_on: t2", shown.stdout)

        deleted = self.planner_command("task", "delete", "t1")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)

        # t3 still depends on t2, so t2 stays protected until t3 goes first.
        refused = self.planner_command("task", "delete", "t2")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("t3 depends on it", refused.stderr)

        deleted = self.planner_command("task", "delete", "t3")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        deleted = self.planner_command("task", "delete", "t2")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(self.planner_command("task", "list").stdout, "")

        # The planner never touched a source file.
        self.assertEqual((self.src / "main.py").read_bytes(), source_before)
        self.assertEqual(
            sorted(path.name for path in self.src.iterdir()), ["main.py"]
        )

    def test_task_changes_persist_in_sqlite(self):
        self.planner_command(
            "task", "create", "--title", "One", "--description", "First task.",
        )
        self.planner_command(
            "task", "create", "--title", "Two", "--depends-on", "t1",
        )
        self.planner_command("task", "update", "t2", "--title", "Two updated")
        rejected = self.planner_command("task", "update", "t1", "--depends-on", "t2")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("cycle", rejected.stderr)

        conn = sqlite3.connect(str(self.task_db()))
        try:
            tasks = conn.execute(
                "SELECT id, title, description, status FROM tasks ORDER BY id"
            ).fetchall()
            edges = conn.execute(
                "SELECT task_id, depends_on_id FROM dependencies ORDER BY task_id"
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(
            tasks,
            [
                (1, "One", "First task.", "PENDING"),
                (2, "Two updated", "", "PENDING"),
            ],
        )
        self.assertEqual(edges, [(2, 1)])

        # A fresh CLI process reads the persisted state back.
        self.assertIn("title: Two updated", self.planner_command("task", "show", "t2").stdout)

    def test_task_work_does_not_disturb_the_planner_session(self):
        session_before = self.planner_session_id()
        self.planner_command("task", "create", "--title", "One")
        self.planner_command("task", "list")

        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resuming planner session", result.stdout)
        self.assertEqual(self.planner_session_id(), session_before)
        for call in self.tui_calls():
            self.assertNotIn("--prompt", call["argv"])

    def test_planner_cannot_redirect_task_output(self):
        # Anything not pre-approved makes the workflow helper refuse to run,
        # and a redirected statement is denied outright, never asked.
        self.assertEqual(
            self.ruleset.effect("shell", "actl task list > src/main.py"), "deny"
        )
        with self.assertRaises(AssertionError):
            self.planner_command("task", "list", ">", "src/main.py")
        self.assertEqual(
            (self.src / "main.py").read_text(encoding="utf-8"), "print('hello')\n"
        )


if __name__ == "__main__":
    unittest.main()
