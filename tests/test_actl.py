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

from actl.cli import PLANNER_PROMPT  # noqa: E402

FAKE_OPENCODE = '''#!python
"""Fake OpenCode CLI used by actl's test suite.

Behaviour is controlled through environment variables:

* ``ACTL_FAKE_LOG``         - JSONL file receiving {"argv": [...], "cwd": ...}
* ``ACTL_FAKE_SESSION_ID``  - session id returned when creating a session
* ``ACTL_FAKE_CREATE_EXIT`` - non-zero to make session creation fail
* ``ACTL_FAKE_CREATE_DIR``  - override the reported session directory on create
* ``ACTL_FAKE_GET_EXIT``    - non-zero to make session lookup fail
* ``ACTL_FAKE_GET_DIR``     - override the reported session directory on lookup
* ``ACTL_FAKE_TUI_EXIT``    - exit code for the interactive TUI invocation
"""

import json
import os
import sys


def _log(argv):
    path = os.environ.get("ACTL_FAKE_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\\n")


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
            directory = (
                os.environ.get("ACTL_FAKE_CREATE_DIR")
                or body.get("location", {}).get("directory")
                or os.getcwd()
            )
            session_id = os.environ.get(
                "ACTL_FAKE_SESSION_ID", "ses_fake0000000000000000000"
            )
            print(json.dumps({"data": {"id": session_id,
                                       "location": {"directory": directory}}}))
            return 0

        if method == "GET" and path.startswith("/api/session/"):
            code = int(os.environ.get("ACTL_FAKE_GET_EXIT", "0"))
            if code:
                print("fake opencode: session not found", file=sys.stderr)
                return code
            session_id = path.rsplit("/", 1)[-1]
            directory = os.environ.get("ACTL_FAKE_GET_DIR") or os.getcwd()
            print(json.dumps({"data": {"id": session_id,
                                       "location": {"directory": directory}}}))
            return 0

    # Anything else is treated as the interactive TUI invocation.
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

    def tui_calls(self):
        return [c for c in self.calls() if c["argv"][0] != "api"]

    def metadata_path(self) -> Path:
        return self.repo / ".actl" / "project.json"

    def planner_path(self) -> Path:
        return self.repo / ".actl" / "planner.json"

    def planner_session_id(self) -> str:
        return json.loads(self.planner_path().read_text(encoding="utf-8"))["session_id"]

    def create_body(self, call):
        argv = call["argv"]
        return json.loads(argv[argv.index("--data") + 1])


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

        tuis = self.tui_calls()
        self.assertEqual(len(tuis), 1)
        argv = tuis[0]["argv"]
        self.assertEqual(argv[0], str(self.repo))
        self.assertEqual(tuis[0]["cwd"], str(self.repo))
        self.assertEqual(argv[argv.index("--session") + 1], "ses_faketest0000000000000001")
        self.assertEqual(argv[argv.index("--prompt") + 1], PLANNER_PROMPT)
        self.assertEqual(self.planner_session_id(), "ses_faketest0000000000000001")

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
        self.assertNotIn("--prompt", argv, "resumed sessions are not re-prompted")

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

        argv = self.tui_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--session") + 1], second_id)
        self.assertIn("--prompt", argv)
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


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlanGuardTests(ActlTestCase):
    def test_plan_before_init_does_not_launch_opencode(self):
        self.init_git()
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("actl init", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_plan_with_missing_metadata(self):
        self.init_git()
        (self.repo / ".actl").mkdir()
        result = self.run_actl("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid actl project metadata", result.stderr)
        self.assertEqual(self.calls(), [])

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


if __name__ == "__main__":
    unittest.main()
