"""End-to-end tests for actl Stage 1.

All OpenCode interactions run through a fake ``opencode`` executable placed on
PATH, so the suite uses no real OpenCode process, no background service, and no
model calls.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from actl.cli import PLANNER_AGENT_DOC, PLANNER_AGENT_NAME  # noqa: E402

FAKE_OPENCODE = '''#!python
"""Fake OpenCode CLI used by actl's test suite.

Behaviour is controlled through environment variables:

* ``ACTL_FAKE_LOG``             - JSONL file receiving {"argv": [...], "cwd": ...}
* ``ACTL_FAKE_SESSION_ID``      - session id returned when creating a session
* ``ACTL_FAKE_CREATE_EXIT``     - non-zero to make session creation fail
* ``ACTL_FAKE_CREATE_DIR``      - override the reported session directory on create
* ``ACTL_FAKE_CREATE_LOCATION`` - raw JSON for the reported session `location`
* ``ACTL_FAKE_CREATE_AGENT``    - override the agent reported by session creation
* ``ACTL_FAKE_GET_EXIT``        - non-zero to make session lookup fail
* ``ACTL_FAKE_GET_DIR``     - override the reported session directory on lookup
* ``ACTL_FAKE_GET_AGENT``   - agent reported by session lookup
* ``ACTL_FAKE_SWITCH_EXIT`` - non-zero to make agent switching fail
* ``ACTL_FAKE_TUI_EXIT``    - exit code for the interactive TUI invocation
* ``ACTL_FAKE_RUN``         - worker behaviour on ``run``: commit (default),
                              dirty (leave an uncommitted file), none, empty
                              (make an empty commit), or detach (leave the
                              worktree on a detached HEAD)
* ``ACTL_FAKE_RUN_EXIT``    - exit code for the noninteractive run
* ``ACTL_FAKE_RUN_MARKER``  - file receiving the worker PID when it starts
* ``ACTL_FAKE_RUN_WAIT_FOR`` - the worker waits for this file (max 120s)
                              after writing the marker and before doing its
                              work
* ``ACTL_FAKE_RUN_DONE``    - file written when the worker is about to exit
"""

import json
import os
import subprocess
import sys
import time


def _log(argv):
    path = os.environ.get("ACTL_FAKE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\\n")


def _run_worker():
    """Emulate one noninteractive worker turn inside the current directory."""
    marker = os.environ.get("ACTL_FAKE_RUN_MARKER")
    if marker:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    wait_for = os.environ.get("ACTL_FAKE_RUN_WAIT_FOR")
    deadline = time.time() + 120
    while wait_for and not os.path.exists(wait_for) and time.time() < deadline:
        time.sleep(0.05)
    behavior = os.environ.get("ACTL_FAKE_RUN", "commit")
    if behavior == "commit":
        with open("worker-output.txt", "a", encoding="utf-8") as handle:
            handle.write("worker change\\n")
        subprocess.run(["git", "add", "worker-output.txt"], check=True)
        subprocess.run(
            ["git", "-c", "user.name=Fake Worker",
             "-c", "user.email=worker@example.invalid",
             "commit", "-m", "worker: implement task"],
            check=True, capture_output=True,
        )
    elif behavior == "dirty":
        with open("worker-output.txt", "a", encoding="utf-8") as handle:
            handle.write("uncommitted work\\n")
    elif behavior == "empty":
        subprocess.run(
            ["git", "-c", "user.name=Fake Worker",
             "-c", "user.email=worker@example.invalid",
             "commit", "--allow-empty", "-m", "worker: empty commit"],
            check=True, capture_output=True,
        )
    elif behavior == "detach":
        subprocess.run(["git", "checkout", "-q", "--detach"], check=True)
    print("fake opencode: worker turn finished")
    print("fake opencode: worker detail", file=sys.stderr)
    done = os.environ.get("ACTL_FAKE_RUN_DONE")
    if done:
        with open(done, "w", encoding="utf-8") as handle:
            handle.write("done")
    return int(os.environ.get("ACTL_FAKE_RUN_EXIT", "0"))


def main():
    argv = sys.argv[1:]
    _log(argv)

    if len(argv) >= 3 and argv[0] == "api":
        method, path = argv[1], argv[2]

        if method == "POST" and path == "/api/session":
            code = int(os.environ.get("ACTL_FAKE_CREATE_EXIT", "0"))
            if code:
                print("fake opencode: session creation failed", file=sys.stderr)
                return code
            body = {}
            if "--data" in argv:
                body = json.loads(argv[argv.index("--data") + 1])
            if "ACTL_FAKE_CREATE_LOCATION" in os.environ:
                location = json.loads(os.environ["ACTL_FAKE_CREATE_LOCATION"])
            else:
                directory = (
                    os.environ.get("ACTL_FAKE_CREATE_DIR")
                    or body.get("location", {}).get("directory")
                    or os.getcwd()
                )
                location = {"directory": directory}
            session_id = os.environ.get(
                "ACTL_FAKE_SESSION_ID", "ses_fake0000000000000000000"
            )
            agent = os.environ.get("ACTL_FAKE_CREATE_AGENT") or body.get("agent")
            print(json.dumps({"data": {"id": session_id,
                                       "agent": agent,
                                       "location": location}}))
            return 0

        if method == "POST" and path.startswith("/api/session/") and path.endswith("/agent"):
            code = int(os.environ.get("ACTL_FAKE_SWITCH_EXIT", "0"))
            if code:
                print("fake opencode: agent switch failed", file=sys.stderr)
                return code
            return 0

        if method == "GET" and path.startswith("/api/session/"):
            code = int(os.environ.get("ACTL_FAKE_GET_EXIT", "0"))
            if code:
                print("fake opencode: session not found", file=sys.stderr)
                return code
            session_id = path.rsplit("/", 1)[-1]
            directory = os.environ.get("ACTL_FAKE_GET_DIR") or os.getcwd()
            agent = os.environ.get("ACTL_FAKE_GET_AGENT", "actl-planner")
            print(json.dumps({"data": {"id": session_id,
                                       "agent": agent,
                                       "location": {"directory": directory}}}))
            return 0

    if argv and argv[0] == "run":
        return _run_worker()

    # Anything after the api and run handling is treated as the interactive
    # TUI invocation.
    return int(os.environ.get("ACTL_FAKE_TUI_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
'''

MINIMAL_PATH = "/usr/bin:/bin"


class ActlTestCase(unittest.TestCase):
    """Shared fixtures: a temporary directory, a fake opencode, env control."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="actl-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fake = self.bin / "opencode"
        fake.write_text(
            FAKE_OPENCODE.replace("#!python", "#!" + sys.executable, 1),
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.log = self.base / "calls.jsonl"
        self.repo = self.base / "repo"
        self.repo.mkdir()

        self.env = os.environ.copy()
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")
        self.env["PYTHONPATH"] = str(SRC)
        self.env["ACTL_FAKE_LOG"] = str(self.log)
        for key in list(self.env):
            if key.startswith("ACTL_FAKE_") and key != "ACTL_FAKE_LOG":
                del self.env[key]

    # -- helpers ---------------------------------------------------------

    def run_actl(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, "-m", "actl", *args],
            cwd=str(cwd or self.repo),
            env=self.env,
            capture_output=True,
            text=True,
        )

    def init_git(self):
        subprocess.run(
            ["git", "init", "-q", str(self.repo)],
            check=True,
            capture_output=True,
        )

    def initialize(self):
        self.init_git()
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)

    def calls(self):
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def create_calls(self):
        return [c for c in self.calls() if c["argv"][:3] == ["api", "POST", "/api/session"]]

    def switch_calls(self):
        return [
            c
            for c in self.calls()
            if c["argv"][:2] == ["api", "POST"] and c["argv"][2].endswith("/agent")
        ]

    def tui_calls(self):
        return [c for c in self.calls() if c["argv"][0] != "api"]

    def metadata_path(self) -> Path:
        return self.repo / ".actl" / "project.json"

    def planner_path(self) -> Path:
        return self.repo / ".actl" / "planner.json"

    def agent_path(self) -> Path:
        return self.repo / ".opencode" / "agents" / "actl-planner.md"

    def planner_session_id(self) -> str:
        return json.loads(self.planner_path().read_text(encoding="utf-8"))["session_id"]

    def create_body(self, call):
        argv = call["argv"]
        return json.loads(argv[argv.index("--data") + 1])

    def planner_rules(self):
        """Parse the planner agent's permission rules into a list of dicts."""
        lines = self.agent_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "---", "agent file needs frontmatter")
        end = lines.index("---", 1)
        rules = []
        rule = None
        for line in lines[1:end]:
            key, _, value = line.strip().partition(":")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key == "- action":
                rule = {"action": value}
                rules.append(rule)
            elif key in ("resource", "effect") and rule is not None:
                rule[key] = value
        return rules


@unittest.skipUnless(shutil.which("git"), "git is required")
class InitTests(ActlTestCase):
    def test_init_creates_metadata_and_gitignore(self):
        self.init_git()
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Initialized actl", result.stdout)

        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["name"], "repo")
        self.assertIn("created_at", metadata)

        gitignore = (self.repo / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".actl/", gitignore.splitlines())

    def test_init_outside_git_repository(self):
        result = self.run_actl("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git repository", result.stderr)
        self.assertFalse((self.repo / ".actl").exists())
        self.assertEqual(self.calls(), [])

    def test_repeated_initialization_is_graceful(self):
        (self.repo / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
        self.initialize()

        metadata_before = self.metadata_path().read_text(encoding="utf-8")
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already initialized", result.stdout)
        self.assertEqual(self.metadata_path().read_text(encoding="utf-8"), metadata_before)

        gitignore = (self.repo / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("*.log", gitignore)
        self.assertIn("build/", gitignore)
        self.assertEqual(gitignore.splitlines().count(".actl/"), 1)

    def test_init_without_gitignore_file(self):
        self.initialize()
        self.assertEqual(
            (self.repo / ".gitignore").read_text(encoding="utf-8").splitlines(),
            [".actl/"],
        )

    def test_init_preserves_gitignore_bytes_and_crlf(self):
        before = b"# Windows project\r\nbuild/\r\n"
        (self.repo / ".gitignore").write_bytes(before)
        self.initialize()
        self.assertEqual(
            (self.repo / ".gitignore").read_bytes(), before + b".actl/\n"
        )

    @unittest.skipIf(os.name == "nt", "trailing spaces in directory names are not portable")
    def test_init_preserves_repository_names_with_trailing_space(self):
        spaced = self.base / "repo "
        spaced.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(spaced)], check=True, capture_output=True
        )
        result = self.run_actl("init", cwd=spaced)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((spaced / ".actl" / "project.json").is_file())
        self.assertFalse(
            (self.repo / ".actl").exists(),
            "init must not write into the sibling directory 'repo'",
        )

    def test_init_gitignore_with_non_utf8_bytes(self):
        before = b"\xff\xfe legacy\n"
        (self.repo / ".gitignore").write_bytes(before)
        self.initialize()
        self.assertEqual(
            (self.repo / ".gitignore").read_bytes(), before + b".actl/\n"
        )

    def test_init_reports_metadata_write_failure(self):
        self.init_git()
        (self.repo / ".actl").write_text("not a directory", encoding="utf-8")
        result = self.run_actl("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".actl", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_init_reports_gitignore_read_failure(self):
        self.init_git()
        (self.repo / ".gitignore").mkdir()
        result = self.run_actl("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".gitignore", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_init_fails_when_opencode_is_missing(self):
        if shutil.which("opencode", path=MINIMAL_PATH):
            self.skipTest("a real opencode executable is visible on the minimal PATH")
        if not shutil.which("git", path=MINIMAL_PATH):
            self.skipTest("git is not available on the minimal PATH")
        self.init_git()
        self.env["PATH"] = MINIMAL_PATH
        result = self.run_actl("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OpenCode is not installed", result.stderr)
        self.assertFalse((self.repo / ".actl").exists())


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlanTests(ActlTestCase):
    def setUp(self):
        super().setUp()
        self.initialize()

    def test_first_planning_session(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000001"
        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Started planner session", result.stdout)

        creates = self.create_calls()
        self.assertEqual(len(creates), 1)
        body = self.create_body(creates[0])
        self.assertEqual(body["location"]["directory"], str(self.repo))
        self.assertEqual(body["agent"], PLANNER_AGENT_NAME)

        tuis = self.tui_calls()
        self.assertEqual(len(tuis), 1)
        argv = tuis[0]["argv"]
        self.assertEqual(argv[0], str(self.repo))
        self.assertEqual(tuis[0]["cwd"], str(self.repo))
        self.assertEqual(argv[argv.index("--session") + 1], "ses_faketest0000000000000001")
        self.assertNotIn("--prompt", argv, "no initial user message may be sent")
        self.assertEqual(self.planner_session_id(), "ses_faketest0000000000000001")
        self.assertEqual(self.switch_calls(), [], "fresh sessions already use the planner agent")

    def test_plan_installs_read_only_planner_agent(self):
        self.assertEqual(self.run_actl("plan").returncode, 0)
        content = self.agent_path().read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\n"), "agent file needs frontmatter")
        self.assertIn("mode: primary", content)
        self.assertIn("planning assistant for this repository", content)
        self.assertIn("read, glob, and grep", content)
        self.assertIn(
            {"action": "edit", "resource": "*", "effect": "deny"},
            self.planner_rules(),
        )

    def test_plan_agent_denies_everything_not_explicitly_reallowed(self):
        self.assertEqual(self.run_actl("plan").returncode, 0)
        rules = self.planner_rules()
        # OpenCode appends agent rules after its broad base "allow everything"
        # policy and the last matching rule wins, so a catch-all deny must come
        # first. Mutating tools (edit, MCP writers, subagents, web access) stay
        # blocked even though they are not named individually.
        self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
        for rule in rules[1:]:
            self.assertIn(
                (rule["action"], rule["effect"]),
                {
                    ("read", "allow"),
                    ("read", "ask"),
                    ("glob", "allow"),
                    ("grep", "allow"),
                    ("question", "allow"),
                    ("edit", "deny"),
                    ("shell", "ask"),
                    ("shell", "allow"),
                    ("shell", "deny"),
                },
                "unexpected permission rule " + repr(rule),
            )
        # Only task management may be pre-approved. A broad task deny blocks
        # execution (and formatting variants) before the management allows.
        management = {
            "actl task create *",
            "actl task list *",
            "actl task show *",
            "actl task update *",
            "actl task delete *",
        }
        for rule in rules:
            if rule["action"] == "shell" and rule["effect"] == "allow":
                self.assertIn(rule["resource"], management)
            if rule["action"] == "shell" and rule["effect"] == "deny":
                self.assertIn(rule["resource"], ("actl task *", "*>*"))
        # The .env asks must follow the broad read allow, otherwise the
        # last-match-wins rule would silently turn them into plain allows.
        broad_read = rules.index({"action": "read", "resource": "*", "effect": "allow"})
        env_ask = rules.index({"action": "read", "resource": "*.env", "effect": "ask"})
        self.assertLess(broad_read, env_ask)

    def test_plan_agent_preapproves_only_task_commands(self):
        self.assertEqual(self.run_actl("plan").returncode, 0)
        rules = self.planner_rules()
        # Only the task management subcommands are pre-approved, so the planner
        # can manage tasks without a prompt on every command...
        management_allows = [
            {"action": "shell", "resource": "actl task " + command + " *",
             "effect": "allow"}
            for command in ("create", "list", "show", "update", "delete")
        ]
        for shell_allow in management_allows:
            self.assertIn(shell_allow, rules)
        ask = {"action": "shell", "resource": "*", "effect": "ask"}
        self.assertIn(ask, rules)
        redirect_deny = {"action": "shell", "resource": "*>*", "effect": "deny"}
        self.assertIn(redirect_deny, rules)
        # ...while executing tasks (including whitespace variants) is denied
        # by the broad task deny that comes before those allows.
        task_deny = {"action": "shell", "resource": "actl task *", "effect": "deny"}
        self.assertIn(task_deny, rules)
        self.assertLess(rules.index(task_deny), rules.index(management_allows[0]))
        # ...while the rules stay effective: last match wins, so the broad ask
        # must come before the task deny, and the redirect deny (which blocks
        # "actl task ... > file" silently overwriting files) must come after
        # the task allows. The catch-all deny comes first.
        self.assertLess(rules.index(ask), rules.index(task_deny))
        for shell_allow in management_allows:
            self.assertLess(rules.index(shell_allow), rules.index(redirect_deny))
        content = self.agent_path().read_text(encoding="utf-8")
        self.assertIn("actl task create", content)
        self.assertIn("checked separately", content)
        self.assertIn("never open .actl/tasks.db", content)

    def test_plan_installs_planner_agent_once(self):
        first = self.run_actl("plan")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("Updated the planner agent", first.stdout)
        mtime = self.agent_path().stat().st_mtime_ns

        second = self.run_actl("plan")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("Updated the planner agent", second.stdout)
        self.assertEqual(self.agent_path().stat().st_mtime_ns, mtime)

    def test_plan_refreshes_stale_planner_agent(self):
        legacy = (
            "---\n"
            "description: old planner\n"
            "mode: primary\n"
            "permissions:\n"
            '  - action: edit\n    resource: "*"\n    effect: deny\n'
            '  - action: shell\n    resource: "*"\n    effect: ask\n'
            '  - action: shell\n    resource: "git status *"\n    effect: allow\n'
            "---\n"
            "old planner body\n"
        )
        self.agent_path().parent.mkdir(parents=True)
        self.agent_path().write_text(legacy, encoding="utf-8")

        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the planner agent", result.stdout)
        content = self.agent_path().read_text(encoding="utf-8")
        self.assertNotIn("old planner body", content)
        self.assertNotIn("git status", content, "stale pre-approvals must be removed")
        self.assertEqual(content, PLANNER_AGENT_DOC)

    def test_resumes_existing_session(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000002"
        self.assertEqual(self.run_actl("plan").returncode, 0)

        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resuming planner session", result.stdout)

        self.assertEqual(len(self.create_calls()), 1, "no new session may be created")
        tuis = self.tui_calls()
        self.assertEqual(len(tuis), 2)
        argv = tuis[1]["argv"]
        self.assertEqual(argv[argv.index("--session") + 1], "ses_faketest0000000000000002")
        self.assertNotIn("--prompt", argv, "resumed sessions must not be prompted")
        self.assertEqual(
            self.switch_calls(), [], "sessions already using the planner agent are left alone"
        )

    def test_plan_never_sends_an_initial_prompt(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000011"
        self.assertEqual(self.run_actl("plan").returncode, 0)
        self.assertEqual(self.run_actl("plan").returncode, 0)
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000012"
        self.assertEqual(self.run_actl("plan", "--new").returncode, 0)
        self.assertEqual(len(self.tui_calls()), 3)
        for call in self.tui_calls():
            self.assertNotIn("--prompt", call["argv"])

    def test_plan_switches_legacy_session_agent(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000010"
        self.assertEqual(self.run_actl("plan").returncode, 0)

        self.env["ACTL_FAKE_GET_AGENT"] = "build"  # session created before agents existed
        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)

        switches = self.switch_calls()
        self.assertEqual(len(switches), 1)
        argv = switches[0]["argv"]
        self.assertEqual(argv[2], "/api/session/ses_faketest0000000000000010/agent")
        self.assertEqual(json.loads(argv[argv.index("--data") + 1]), {"agent": PLANNER_AGENT_NAME})

        calls = self.calls()
        switch_positions = [i for i, c in enumerate(calls) if c in switches]
        tui_positions = [i for i, c in enumerate(calls) if c["argv"][0] != "api"]
        self.assertEqual(len(tui_positions), 2)
        self.assertLess(switch_positions[0], tui_positions[1], "switch before launch")

    def test_plan_reports_agent_switch_failure(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000013"
        self.assertEqual(self.run_actl("plan").returncode, 0)

        self.env["ACTL_FAKE_GET_AGENT"] = "build"
        self.env["ACTL_FAKE_SWITCH_EXIT"] = "1"
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot select the planner agent", result.stderr)
        self.assertEqual(len(self.tui_calls()), 1, "OpenCode must not be launched")

    def test_plan_agent_install_failure_is_reported(self):
        self.agent_path().mkdir(parents=True)  # a directory cannot be overwritten
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not write the planner agent", result.stderr)
        self.assertEqual(self.tui_calls(), [])

    def test_plan_new_starts_a_separate_session(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000003"
        self.assertEqual(self.run_actl("plan").returncode, 0)
        first_id = self.planner_session_id()

        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000004"
        result = self.run_actl("plan", "--new")
        self.assertEqual(result.returncode, 0, result.stderr)

        second_id = self.planner_session_id()
        self.assertEqual(second_id, "ses_faketest0000000000000004")
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(self.create_calls()), 2)
        self.assertEqual(self.create_body(self.create_calls()[1])["agent"], PLANNER_AGENT_NAME)

        argv = self.tui_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--session") + 1], second_id)
        self.assertNotIn("--prompt", argv)
        # previous sessions are preserved: nothing is ever deleted
        self.assertFalse(any("delete" in c["argv"] for c in self.calls()))

    def test_plan_from_subdirectory_uses_project_root(self):
        sub = self.repo / "src" / "nested"
        sub.mkdir(parents=True)
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000008"
        result = self.run_actl("plan", cwd=sub)
        self.assertEqual(result.returncode, 0, result.stderr)
        tui = self.tui_calls()[0]
        self.assertEqual(tui["argv"][0], str(self.repo))
        self.assertEqual(tui["cwd"], str(self.repo))

    @unittest.skipIf(os.name == "nt", "trailing spaces in directory names are not portable")
    def test_plan_does_not_resume_a_sibling_repository(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000015"
        self.assertEqual(self.run_actl("plan").returncode, 0)
        self.assertEqual(len(self.tui_calls()), 1)

        spaced = self.base / "repo "
        spaced.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(spaced)], check=True, capture_output=True
        )
        result = self.run_actl("plan", cwd=spaced)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("actl init", result.stderr)
        self.assertEqual(
            len(self.tui_calls()), 1, "the sibling's session must not be resumed"
        )

    def test_plan_uses_the_opencode_selected_from_the_invocation_directory(self):
        nested = self.repo / "sub"
        nested.mkdir()
        selected = nested / "opencode"
        selected.write_text(
            FAKE_OPENCODE.replace("#!python", "#!" + sys.executable, 1),
            encoding="utf-8",
        )
        selected.chmod(selected.stat().st_mode | stat.S_IXUSR)
        decoy = self.repo / "opencode"
        decoy.write_text("#!/bin/sh\nexit 72\n", encoding="utf-8")
        decoy.chmod(decoy.stat().st_mode | stat.S_IXUSR)
        if not shutil.which("git", path=MINIMAL_PATH):
            self.skipTest("git is not available on the minimal PATH")

        # A relative PATH entry resolves against the invocation directory; the
        # selected executable must keep working after actl switches its
        # subprocesses to the project root.
        self.env["PATH"] = "." + os.pathsep + MINIMAL_PATH
        result = self.run_actl("plan", cwd=nested)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Started planner session", result.stdout)
        self.assertEqual(len(self.tui_calls()), 1)

    def test_plan_does_not_cross_a_nested_git_repository(self):
        child = self.repo / "child-repo"
        child.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(child)], check=True, capture_output=True
        )
        result = self.run_actl("plan", cwd=child)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("actl init", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((child / ".opencode").exists())

    def test_plan_fails_when_opencode_is_missing(self):
        if shutil.which("opencode", path=MINIMAL_PATH):
            self.skipTest("a real opencode executable is visible on the minimal PATH")
        self.env["PATH"] = MINIMAL_PATH
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OpenCode is not installed", result.stderr)
        self.assertEqual(self.tui_calls(), [])

    def test_plan_session_creation_failure(self):
        self.env["ACTL_FAKE_CREATE_EXIT"] = "1"
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not create", result.stderr)
        self.assertFalse(self.planner_path().exists())
        self.assertEqual(self.tui_calls(), [])

    def test_plan_session_created_in_wrong_directory_is_rejected(self):
        self.env["ACTL_FAKE_CREATE_DIR"] = str(self.base / "elsewhere")
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("belongs to", result.stderr)
        self.assertFalse(self.planner_path().exists())
        self.assertEqual(self.tui_calls(), [])

    def test_plan_tui_failure_is_reported(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000005"
        self.env["ACTL_FAKE_TUI_EXIT"] = "7"
        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 7)
        self.assertIn("exited with status 7", result.stderr)
        # the session exists, so a later run can resume it
        self.assertEqual(self.planner_session_id(), "ses_faketest0000000000000005")

    def test_plan_refuses_unverifiable_session(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000006"
        self.assertEqual(self.run_actl("plan").returncode, 0)
        self.assertEqual(len(self.tui_calls()), 1)

        self.env["ACTL_FAKE_GET_EXIT"] = "1"
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot verify planner session", result.stderr)
        self.assertIn("--new", result.stderr)
        self.assertEqual(len(self.tui_calls()), 1, "OpenCode must not be launched")

    def test_plan_refuses_session_from_another_directory(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000007"
        self.assertEqual(self.run_actl("plan").returncode, 0)

        self.env["ACTL_FAKE_GET_DIR"] = str(self.base / "elsewhere")
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("belongs to", result.stderr)
        self.assertEqual(len(self.tui_calls()), 1, "OpenCode must not be launched")

    def test_plan_with_corrupt_planner_state(self):
        self.planner_path().write_text("{not json", encoding="utf-8")
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--new", result.stderr)
        self.assertEqual(self.tui_calls(), [])

    def test_plan_with_undecodable_planner_state(self):
        self.planner_path().write_bytes(b"\xff\xfe")
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot resume the planner session", result.stderr)
        self.assertIn("--new", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.tui_calls(), [])

    def test_plan_new_ignores_undecodable_state(self):
        self.planner_path().write_bytes(b"\xff\xfe")
        result = self.run_actl("plan", "--new")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_plan_reports_state_write_failure(self):
        self.planner_path().mkdir()
        result = self.run_actl("plan", "--new")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not save the planner session state", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(
            self.tui_calls(), [], "the TUI must not launch without a saved session"
        )

    def test_plan_rejects_malformed_session_location(self):
        self.env["ACTL_FAKE_CREATE_LOCATION"] = json.dumps({"directory": ["bad"]})
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected session directory", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.planner_path().exists())
        self.assertEqual(self.tui_calls(), [])

    def test_plan_switches_agent_on_new_session_when_needed(self):
        self.env["ACTL_FAKE_SESSION_ID"] = "ses_faketest0000000000000014"
        self.env["ACTL_FAKE_CREATE_AGENT"] = "build"
        result = self.run_actl("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        switches = self.switch_calls()
        self.assertEqual(len(switches), 1)
        argv = switches[0]["argv"]
        self.assertEqual(argv[2], "/api/session/ses_faketest0000000000000014/agent")
        self.assertEqual(
            json.loads(argv[argv.index("--data") + 1]), {"agent": PLANNER_AGENT_NAME}
        )
        self.assertEqual(self.planner_session_id(), "ses_faketest0000000000000014")
        self.assertEqual(len(self.tui_calls()), 1)


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlanGuardTests(ActlTestCase):
    def test_plan_before_init_does_not_launch_opencode(self):
        self.init_git()
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("actl init", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.repo / ".opencode").exists())

    def test_plan_with_missing_metadata(self):
        self.init_git()
        (self.repo / ".actl").mkdir()
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid actl project metadata", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.repo / ".opencode").exists())

    def test_plan_with_invalid_metadata(self):
        self.init_git()
        (self.repo / ".actl").mkdir()
        payloads = [
            "not json at all",
            json.dumps([]),
            json.dumps({}),
            json.dumps({"schema_version": 99, "name": "repo"}),
            json.dumps({"schema_version": 1, "name": 42}),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.metadata_path().write_text(payload, encoding="utf-8")
                result = self.run_actl("plan")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid actl project metadata", result.stderr)
                self.assertEqual(self.calls(), [])
                self.assertFalse((self.repo / ".opencode").exists())

    def test_plan_with_undecodable_metadata(self):
        self.init_git()
        (self.repo / ".actl").mkdir()
        (self.repo / ".actl" / "project.json").write_bytes(b"\xff\xfe")
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid actl project metadata", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_plan_outside_git_repository(self):
        outside = self.base / "outside"
        outside.mkdir()
        result = self.run_actl("plan", cwd=outside)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git repository", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_plan_ignores_actl_metadata_outside_git(self):
        outside = self.base / "copied"
        (outside / ".actl").mkdir(parents=True)
        (outside / ".actl" / "project.json").write_text(
            json.dumps({"schema_version": 1, "name": "copied", "created_at": "x"}),
            encoding="utf-8",
        )
        result = self.run_actl("plan", cwd=outside)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git repository", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((outside / ".opencode").exists())


if __name__ == "__main__":
    unittest.main()
