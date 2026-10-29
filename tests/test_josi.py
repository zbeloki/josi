"""End-to-end tests for josi Stage 1.

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

from josi.cli import PLANNER_AGENT_DOC, PLANNER_AGENT_NAME  # noqa: E402

FAKE_OPENCODE = '''#!python
"""Fake OpenCode CLI used by josi's test suite.

Behaviour is controlled through environment variables:

* ``JOSI_FAKE_LOG``             - JSONL file receiving {"argv": [...], "cwd": ...}
* ``JOSI_FAKE_SESSION_ID``      - session id returned when creating a session
* ``JOSI_FAKE_CREATE_EXIT``     - non-zero to make session creation fail
* ``JOSI_FAKE_CREATE_DIR``      - override the reported session directory on create
* ``JOSI_FAKE_CREATE_LOCATION`` - raw JSON for the reported session `location`
* ``JOSI_FAKE_CREATE_AGENT``    - override the agent reported by session creation
* ``JOSI_FAKE_GET_EXIT``        - non-zero to make session lookup fail
* ``JOSI_FAKE_GET_DIR``     - override the reported session directory on lookup
* ``JOSI_FAKE_GET_AGENT``   - agent reported by session lookup
* ``JOSI_FAKE_SWITCH_EXIT`` - non-zero to make agent switching fail
* ``JOSI_FAKE_TUI_EXIT``    - exit code for the interactive TUI invocation
* ``JOSI_FAKE_RUN``         - worker behaviour on ``run``: commit (default),
                              dirty (leave an uncommitted file), none, empty
                              (make an empty commit), detach (leave the
                              worktree on a detached HEAD), commit-index
                              (commit a new ``worker-output-N.txt`` file,
                              with N the one-based invocation number from
                              ``JOSI_FAKE_RUN_COUNT_FILE``), or
                              ignore-collision (drop the ``local-notes.txt``
                              ignore rule and commit a tracked
                              ``local-notes.txt``)
* ``JOSI_FAKE_RUN_EXIT``    - exit code for the noninteractive run
* ``JOSI_FAKE_RUN_SEQUENCE`` - JSON list of per-invocation behaviours (a
                              string behaviour or an object with optional
                              ``behavior``, ``exit``, and ``wait_for``
                              members); the invocation number from
                              ``JOSI_FAKE_RUN_COUNT_FILE`` selects the entry,
                              and entries past the end fall back to the
                              environment defaults
* ``JOSI_FAKE_RUN_COUNT_FILE`` - file counting worker invocations; used with
                              ``JOSI_FAKE_RUN_SEQUENCE`` and ``commit-index``
* ``JOSI_FAKE_RUN_STREAM``  - text the worker prints to stdout and stderr
                              (flushed immediately) before it waits or does
                              any work, to test that the log is written
                              while the worker is still running
* ``JOSI_FAKE_RUN_MARKER``  - file receiving the worker PID when it starts
* ``JOSI_FAKE_RUN_WAIT_FOR`` - the worker waits for this file (max 120s)
                              after writing the marker and before doing its
                              work
* ``JOSI_FAKE_RUN_DONE``    - file written when the worker is about to exit
* ``JOSI_FAKE_REVIEW_JSON`` - the final reviewer message (defaults to an
                              approval); ``opencode run --agent plan`` prints
                              it as the text event of ``--format json`` output
* ``JOSI_FAKE_REVIEW_SEQUENCE`` - JSON list of per-invocation reviewer
                              messages (objects) selected with
                              ``JOSI_FAKE_REVIEW_COUNT_FILE``; entries past
                              the end fall back to ``JOSI_FAKE_REVIEW_JSON``
* ``JOSI_FAKE_REVIEW_COUNT_FILE`` - file counting reviewer invocations; used
                              with ``JOSI_FAKE_REVIEW_SEQUENCE``
* ``JOSI_FAKE_REVIEW_EXIT`` - exit code for the reviewer invocation
* ``JOSI_FAKE_REVIEW_MARKER`` - file receiving the reviewer PID when it starts
* ``JOSI_FAKE_REVIEW_WAIT_FOR`` - the reviewer waits for this file (max 120s)
* ``JOSI_FAKE_REVIEW_DONE`` - file written when the reviewer is about to exit
* ``JOSI_FAKE_REVIEW_MUTATE`` - reviewer-side change to its own checkout:
                              ``commit`` (replace a tracked file and commit),
                              ``dirty`` (modify a tracked file), or
                              ``untracked`` (write a scratch file)
* ``JOSI_FAKE_TESTS``       - test-writer behaviour on ``opencode run
                              --format json`` with the build agent: none
                              (default; change nothing and decide that no
                              tests are needed), commit (commit a new
                              ``test-output.txt``), dirty (leave an
                              uncommitted file), untracked (leave an
                              untracked file), or empty (make an empty
                              commit)
* ``JOSI_FAKE_TESTS_JSON``  - the final test-writer message (defaults to a
                              ``no_tests_needed`` decision)
* ``JOSI_FAKE_TESTS_EXIT``  - exit code for the test-writer invocation
* ``JOSI_FAKE_TESTS_SEQUENCE`` - JSON list of per-invocation test-writer
                              behaviours (a string behaviour or an object
                              with optional ``behavior``, ``exit``, and
                              ``message`` members) selected with
                              ``JOSI_FAKE_TESTS_COUNT_FILE``; entries past
                              the end fall back to ``JOSI_FAKE_TESTS``
* ``JOSI_FAKE_TESTS_COUNT_FILE`` - file counting test-writer invocations;
                              used with ``JOSI_FAKE_TESTS_SEQUENCE``
* ``JOSI_FAKE_SESSION_USAGE`` - JSON object with the cumulative ``cost`` and
                              ``tokens`` reported by every session lookup
* ``JOSI_FAKE_SESSION_USAGE_SEQUENCE`` - JSON list of per-lookup cumulative
                              usage objects selected with
                              ``JOSI_FAKE_SESSION_USAGE_COUNT_FILE``; entries
                              past the end fall back to
                              ``JOSI_FAKE_SESSION_USAGE``
* ``JOSI_FAKE_SESSION_USAGE_COUNT_FILE`` - file counting session lookups; used
                              with ``JOSI_FAKE_SESSION_USAGE_SEQUENCE``
* ``JOSI_FAKE_MODEL_LIST`` - JSON list of model objects returned by
                              ``GET /api/model`` (pricing metadata)
"""

import json
import os
import subprocess
import sys
import time


def _log(argv):
    path = os.environ.get("JOSI_FAKE_LOG")
    if not path:
        return
    env = {
        key: value for key, value in os.environ.items()
        if key.startswith("OPENCODE_")
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"argv": argv, "cwd": os.getcwd(), "env": env}) + "\\n"
        )


def _next_index(path):
    """Return the current invocation index and bump the count file.

    Without a count file every invocation is index 0, which keeps the
    environment-default behaviour.
    """
    if not path:
        return 0
    try:
        with open(path, encoding="utf-8") as handle:
            index = int(handle.read().strip() or "0")
    except (OSError, ValueError):
        index = 0
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(index + 1))
    except OSError:
        pass
    return index


def _sequence_item(name, index):
    """Return the ``index``-th entry of the JSON list in ``name``, or None."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(items, list) or index >= len(items):
        return None
    return items[index]


def _session_usage():
    """Return the cumulative usage reported by one session lookup, or None."""
    index = _next_index(os.environ.get("JOSI_FAKE_SESSION_USAGE_COUNT_FILE"))
    item = _sequence_item("JOSI_FAKE_SESSION_USAGE_SEQUENCE", index)
    if item is not None:
        return item if isinstance(item, dict) else json.loads(item)
    raw = os.environ.get("JOSI_FAKE_SESSION_USAGE")
    if not raw:
        return None
    return json.loads(raw)


def _review_message():
    """The reviewer's final message for this invocation."""
    index = _next_index(os.environ.get("JOSI_FAKE_REVIEW_COUNT_FILE"))
    item = _sequence_item("JOSI_FAKE_REVIEW_SEQUENCE", index)
    if item is not None:
        return item if isinstance(item, str) else json.dumps(item)
    return os.environ.get(
        "JOSI_FAKE_REVIEW_JSON", '{"decision":"approved","findings":[]}'
    )


def _worker_plan():
    """Return ``(behavior, exit_code, wait_for, index)`` for this invocation."""
    behavior = os.environ.get("JOSI_FAKE_RUN", "commit")
    exit_code = int(os.environ.get("JOSI_FAKE_RUN_EXIT", "0"))
    wait_for = os.environ.get("JOSI_FAKE_RUN_WAIT_FOR")
    index = _next_index(os.environ.get("JOSI_FAKE_RUN_COUNT_FILE"))
    item = _sequence_item("JOSI_FAKE_RUN_SEQUENCE", index)
    if isinstance(item, dict):
        behavior = item.get("behavior", behavior)
        exit_code = int(item.get("exit", exit_code))
        wait_for = item.get("wait_for", wait_for)
    elif isinstance(item, str):
        behavior = item
    return behavior, exit_code, wait_for, index


def _tests_plan():
    """Return ``(behavior, exit_code, message)`` for this test-writer turn.

    ``message`` is the final message to print, or None to fall back to
    ``JOSI_FAKE_TESTS_JSON``.
    """
    behavior = os.environ.get("JOSI_FAKE_TESTS", "none")
    exit_code = int(os.environ.get("JOSI_FAKE_TESTS_EXIT", "0"))
    message = None
    index = _next_index(os.environ.get("JOSI_FAKE_TESTS_COUNT_FILE"))
    item = _sequence_item("JOSI_FAKE_TESTS_SEQUENCE", index)
    if isinstance(item, dict):
        behavior = item.get("behavior", behavior)
        exit_code = int(item.get("exit", exit_code))
        message = item.get("message", message)
    elif isinstance(item, str):
        behavior = item
    return behavior, exit_code, message


def _run_test_writer():
    """Emulate one noninteractive test-writer turn in the current directory."""
    behavior, exit_code, message = _tests_plan()
    if behavior == "commit":
        with open("test-output.txt", "a", encoding="utf-8") as handle:
            handle.write("test change\\n")
        subprocess.run(["git", "add", "test-output.txt"], check=True)
        subprocess.run(
            ["git", "-c", "user.name=Fake Test Writer",
             "-c", "user.email=test-writer@example.invalid",
             "commit", "-m", "test writer: add tests"],
            check=True, capture_output=True,
        )
    elif behavior == "dirty":
        with open("test-output.txt", "a", encoding="utf-8") as handle:
            handle.write("uncommitted tests\\n")
    elif behavior == "untracked":
        with open("test-scratch.txt", "w", encoding="utf-8") as handle:
            handle.write("test scratch\\n")
    elif behavior == "empty":
        subprocess.run(
            ["git", "-c", "user.name=Fake Test Writer",
             "-c", "user.email=test-writer@example.invalid",
             "commit", "--allow-empty", "-m", "test writer: empty commit"],
            check=True, capture_output=True,
        )
    print(json.dumps({"type": "step_start",
                      "part": {"type": "step-start"}}))
    if message is None:
        message = os.environ.get(
            "JOSI_FAKE_TESTS_JSON",
            '{"decision":"no_tests_needed","reason":"no tests required"}',
        )
    print(json.dumps({
        "type": "text",
        "part": {"type": "text", "messageID": "msg_fake_tests",
                 "text": message},
    }))
    return exit_code


def _run_reviewer():
    """Emulate one noninteractive reviewer turn with --format json output."""
    marker = os.environ.get("JOSI_FAKE_REVIEW_MARKER")
    if marker:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    wait_for = os.environ.get("JOSI_FAKE_REVIEW_WAIT_FOR")
    deadline = time.time() + 120
    while wait_for and not os.path.exists(wait_for) and time.time() < deadline:
        time.sleep(0.05)
    mutate = os.environ.get("JOSI_FAKE_REVIEW_MUTATE")
    if mutate == "commit":
        with open("worker-output.txt", "w", encoding="utf-8") as handle:
            handle.write("reviewer replacement\\n")
        subprocess.run(["git", "add", "worker-output.txt"], check=True)
        subprocess.run(
            ["git", "-c", "user.name=Fake Reviewer",
             "-c", "user.email=reviewer@example.invalid",
             "commit", "-m", "reviewer: touch the checkout"],
            check=True, capture_output=True,
        )
    elif mutate == "dirty":
        with open("worker-output.txt", "a", encoding="utf-8") as handle:
            handle.write("reviewer edit\\n")
    elif mutate == "untracked":
        with open("reviewer-scratch.txt", "w", encoding="utf-8") as handle:
            handle.write("reviewer scratch\\n")
    print(json.dumps({"type": "step_start",
                      "part": {"type": "step-start"}}))
    message = _review_message()
    print(json.dumps({
        "type": "text",
        "part": {"type": "text", "messageID": "msg_fake_review",
                 "text": message},
    }))
    done = os.environ.get("JOSI_FAKE_REVIEW_DONE")
    if done:
        with open(done, "w", encoding="utf-8") as handle:
            handle.write("done")
    return int(os.environ.get("JOSI_FAKE_REVIEW_EXIT", "0"))


def _run_worker():
    """Emulate one noninteractive worker turn inside the current directory."""
    behavior, exit_code, wait_for, index = _worker_plan()
    marker = os.environ.get("JOSI_FAKE_RUN_MARKER")
    if marker:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    stream = os.environ.get("JOSI_FAKE_RUN_STREAM")
    if stream:
        print("fake opencode: " + stream + " on stdout", flush=True)
        print(
            "fake opencode: " + stream + " on stderr",
            file=sys.stderr, flush=True,
        )
    deadline = time.time() + 120
    while wait_for and not os.path.exists(wait_for) and time.time() < deadline:
        time.sleep(0.05)
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
    elif behavior == "commit-index":
        name = "worker-output-" + str(index + 1) + ".txt"
        with open(name, "a", encoding="utf-8") as handle:
            handle.write("worker change " + str(index + 1) + "\\n")
        subprocess.run(["git", "add", name], check=True)
        subprocess.run(
            ["git", "-c", "user.name=Fake Worker",
             "-c", "user.email=worker@example.invalid",
             "commit", "-m", "worker: implement task"],
            check=True, capture_output=True,
        )
    elif behavior == "ignore-collision":
        with open(".gitignore", encoding="utf-8") as handle:
            lines = [
                line for line in handle
                if line.strip() != "local-notes.txt"
            ]
        with open(".gitignore", "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        with open("local-notes.txt", "w", encoding="utf-8") as handle:
            handle.write("tracked default\\n")
        subprocess.run(
            ["git", "add", ".gitignore", "local-notes.txt"], check=True
        )
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
    done = os.environ.get("JOSI_FAKE_RUN_DONE")
    if done:
        with open(done, "w", encoding="utf-8") as handle:
            handle.write("done")
    return exit_code


def main():
    argv = sys.argv[1:]
    _log(argv)

    if len(argv) >= 3 and argv[0] == "api":
        method, path = argv[1], argv[2]

        if method == "POST" and path == "/api/session":
            code = int(os.environ.get("JOSI_FAKE_CREATE_EXIT", "0"))
            if code:
                print("fake opencode: session creation failed", file=sys.stderr)
                return code
            body = {}
            if "--data" in argv:
                body = json.loads(argv[argv.index("--data") + 1])
            if "JOSI_FAKE_CREATE_LOCATION" in os.environ:
                location = json.loads(os.environ["JOSI_FAKE_CREATE_LOCATION"])
            else:
                directory = (
                    os.environ.get("JOSI_FAKE_CREATE_DIR")
                    or body.get("location", {}).get("directory")
                    or os.getcwd()
                )
                location = {"directory": directory}
            session_id = os.environ.get(
                "JOSI_FAKE_SESSION_ID", "ses_fake0000000000000000000"
            )
            agent = os.environ.get("JOSI_FAKE_CREATE_AGENT") or body.get("agent")
            print(json.dumps({"data": {"id": session_id,
                                       "agent": agent,
                                       "location": location}}))
            return 0

        if method == "POST" and path.startswith("/api/session/") and path.endswith("/agent"):
            code = int(os.environ.get("JOSI_FAKE_SWITCH_EXIT", "0"))
            if code:
                print("fake opencode: agent switch failed", file=sys.stderr)
                return code
            return 0

        if method == "GET" and path.startswith("/api/session/"):
            code = int(os.environ.get("JOSI_FAKE_GET_EXIT", "0"))
            if code:
                print("fake opencode: session not found", file=sys.stderr)
                return code
            session_id = path.rsplit("/", 1)[-1]
            directory = os.environ.get("JOSI_FAKE_GET_DIR") or os.getcwd()
            agent = os.environ.get("JOSI_FAKE_GET_AGENT", "josi-planner")
            data = {"id": session_id,
                    "agent": agent,
                    "location": {"directory": directory}}
            session_usage = _session_usage()
            if session_usage is not None:
                data.update(session_usage)
            print(json.dumps({"data": data}))
            return 0

        if method == "GET" and path == "/api/model":
            raw = os.environ.get("JOSI_FAKE_MODEL_LIST", "[]")
            print(json.dumps({"data": json.loads(raw)}))
            return 0

    if argv and argv[0] == "run":
        if "--agent" in argv and argv[argv.index("--agent") + 1] == "plan":
            return _run_reviewer()
        # The test writer is the noninteractive turn that answers with a
        # JSON decision while using the worker (build) agent.
        if "--format" in argv:
            return _run_test_writer()
        return _run_worker()

    # Anything after the api and run handling is treated as the interactive
    # TUI invocation.
    return int(os.environ.get("JOSI_FAKE_TUI_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
'''

MINIMAL_PATH = "/usr/bin:/bin"


def model_setting(value):
    """Return a stored model (a model/effort pair) as ``name#effort``."""
    if isinstance(value, dict):
        return value["model"] + ("#" + value["effort"] if value["effort"] else "")
    return value


class JosiTestCase(unittest.TestCase):
    """Shared fixtures: a temporary directory, a fake opencode, env control."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="josi-tests-")
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
        self.env["JOSI_FAKE_LOG"] = str(self.log)
        for key in list(self.env):
            if key.startswith("JOSI_FAKE_") and key != "JOSI_FAKE_LOG":
                del self.env[key]
            # OpenCode configuration must not leak in from the invoking
            # environment (for example a parent OpenCode session exporting
            # OPENCODE_CONFIG_CONTENT): tests set what they need explicitly.
            elif key.startswith("OPENCODE_"):
                del self.env[key]

    # -- helpers ---------------------------------------------------------

    def run_josi(self, *args, cwd=None, input_text=""):
        # ``input_text`` defaults to empty but never None: stdin is always a
        # pipe that immediately reaches EOF, so an unexpected interactive
        # prompt fails the test instead of hanging on the terminal.
        return subprocess.run(
            [sys.executable, "-m", "josi", *args],
            cwd=str(cwd or self.repo),
            env=self.env,
            input=input_text,
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
        result = self.run_josi("init")
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
        return self.repo / ".josi" / "project.json"

    def planner_path(self) -> Path:
        return self.repo / ".josi" / "planner.json"

    def agent_path(self) -> Path:
        return self.repo / ".opencode" / "agents" / "josi-planner.md"

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
class InitTests(JosiTestCase):
    def test_init_creates_metadata_and_gitignore(self):
        self.init_git()
        result = self.run_josi("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Initialized Josi", result.stdout)

        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["name"], "repo")
        self.assertIn("created_at", metadata)
        # A repository without commits stores its unborn HEAD branch.
        unborn = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(self.repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(metadata["base_branch"], unborn)
        self.assertIn("Base branch: " + unborn, result.stdout)

        gitignore = (self.repo / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".josi/", gitignore.splitlines())

    def test_init_stores_default_models(self):
        self.init_git()
        result = self.run_josi("init")
        self.assertEqual(result.returncode, 0, result.stderr)

        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(
            model_setting(metadata["worker_model"]), "openrouter/deepseek/deepseek-v4.1-flash#low"
        )
        self.assertEqual(
            model_setting(metadata["reviewer_model"]), "openrouter/openai/gpt-6-luna#high"
        )
        self.assertEqual(
            model_setting(metadata["escalation_worker_model"]), "openrouter/anthropic/claude-sonnet-5.5#medium"
        )
        # The defaults are reported so the user knows what was configured.
        self.assertIn(
            "Worker model: openrouter/deepseek/deepseek-v4.1-flash#low",
            result.stdout,
        )
        self.assertIn(
            "Reviewer model: openrouter/openai/gpt-6-luna#high", result.stdout
        )
        self.assertIn(
            "Escalation worker model: openrouter/anthropic/claude-sonnet-5.5#medium",
            result.stdout,
        )

    def test_init_explicit_model_overrides_only_its_default(self):
        self.init_git()
        result = self.run_josi("init", "--worker-model", "provider/custom")
        self.assertEqual(result.returncode, 0, result.stderr)

        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(model_setting(metadata["worker_model"]), "provider/custom")
        self.assertEqual(
            model_setting(metadata["reviewer_model"]), "openrouter/openai/gpt-6-luna#high"
        )
        self.assertEqual(
            model_setting(metadata["escalation_worker_model"]), "openrouter/anthropic/claude-sonnet-5.5#medium"
        )

    def test_reinit_preserves_models_and_fills_missing_ones(self):
        self.init_git()
        self.assertEqual(
            self.run_josi(
                "init",
                "--worker-model", "provider/worker",
                "--reviewer-model", "provider/reviewer",
                "--escalation-worker-model", "provider/escalation",
            ).returncode,
            0,
        )
        # A plain re-init preserves every stored value.
        before = self.metadata_path().read_bytes()
        result = self.run_josi("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already initialized", result.stdout)
        self.assertEqual(self.metadata_path().read_bytes(), before)

        # Removing one model makes a plain re-init fill only that field.
        metadata = json.loads(before.decode("utf-8"))
        del metadata["reviewer_model"]
        self.metadata_path().write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        result = self.run_josi("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Updated the reviewer model to openrouter/openai/gpt-6-luna#high",
            result.stdout,
        )
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(model_setting(metadata["worker_model"]), "provider/worker")
        self.assertEqual(
            model_setting(metadata["reviewer_model"]), "openrouter/openai/gpt-6-luna#high"
        )
        self.assertEqual(
            model_setting(metadata["escalation_worker_model"]), "provider/escalation"
        )

    def test_reinit_explicit_model_updates_only_that_role(self):
        self.initialize()
        result = self.run_josi("init", "--reviewer-model", "provider/other")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the reviewer model to provider/other", result.stdout)

        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(model_setting(metadata["reviewer_model"]), "provider/other")
        self.assertEqual(
            model_setting(metadata["worker_model"]), "openrouter/deepseek/deepseek-v4.1-flash#low"
        )
        self.assertEqual(
            model_setting(metadata["escalation_worker_model"]), "openrouter/anthropic/claude-sonnet-5.5#medium"
        )

    def test_init_stores_default_test_writer_model(self):
        self.init_git()
        result = self.run_josi("init")
        self.assertEqual(result.returncode, 0, result.stderr)

        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(
            model_setting(metadata["test_writer_model"]),
            "openrouter/z-ai/glm-5.3#high",
        )
        self.assertIn(
            "Test writer model: openrouter/z-ai/glm-5.3#high",
            result.stdout,
        )

    def test_reinit_fills_missing_test_writer_model(self):
        self.initialize()
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        del metadata["test_writer_model"]
        self.metadata_path().write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        result = self.run_josi("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Updated the test writer model to"
            " openrouter/z-ai/glm-5.3#high",
            result.stdout,
        )
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(
            model_setting(metadata["test_writer_model"]),
            "openrouter/z-ai/glm-5.3#high",
        )

    def test_reinit_explicit_test_writer_model_updates_only_that_role(self):
        self.initialize()
        result = self.run_josi("init", "--test-writer-model", "provider/tests")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the test writer model to provider/tests", result.stdout)

        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(model_setting(metadata["test_writer_model"]), "provider/tests")
        self.assertEqual(
            model_setting(metadata["worker_model"]), "openrouter/deepseek/deepseek-v4.1-flash#low"
        )
        self.assertEqual(
            model_setting(metadata["reviewer_model"]), "openrouter/openai/gpt-6-luna#high"
        )

    def test_init_invalid_test_writer_model_is_rejected(self):
        self.init_git()
        for value in ("", "model", "provider/", "/model", "two words/model"):
            with self.subTest(value=value):
                result = self.run_josi("init", "--test-writer-model", value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid test writer model", result.stderr)
        # A leading dash would be read as an option; with the "=" form it
        # reaches josi and is rejected as invalid.
        result = self.run_josi("init", "--test-writer-model=-flag/model")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid test writer model", result.stderr)
        self.assertFalse((self.repo / ".josi").exists())

    def test_init_adds_no_validation_commands(self):
        # Validation commands are the project's own configuration: init
        # records only the models and the base branch and never invents a
        # validation command (no pytest, nothing else), so validation is
        # skipped until the project adds commands itself.
        self.init_git()
        result = self.run_josi("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(
            set(metadata),
            {
                "schema_version", "name", "created_at", "base_branch",
                "agent",
                "worker_model", "reviewer_model", "escalation_worker_model",
                "test_writer_model",
            },
        )

        # A re-init never adds one either.
        self.assertEqual(self.run_josi("init").returncode, 0)
        metadata = json.loads(self.metadata_path().read_text(encoding="utf-8"))
        self.assertNotIn("validation", metadata)

    def test_init_outside_git_repository(self):
        result = self.run_josi("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git repository", result.stderr)
        self.assertFalse((self.repo / ".josi").exists())
        self.assertEqual(self.calls(), [])

    def test_repeated_initialization_is_graceful(self):
        (self.repo / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
        self.initialize()

        metadata_before = self.metadata_path().read_text(encoding="utf-8")
        result = self.run_josi("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already initialized", result.stdout)
        self.assertEqual(self.metadata_path().read_text(encoding="utf-8"), metadata_before)

        gitignore = (self.repo / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("*.log", gitignore)
        self.assertIn("build/", gitignore)
        self.assertEqual(gitignore.splitlines().count(".josi/"), 1)

    def test_init_without_gitignore_file(self):
        self.initialize()
        self.assertEqual(
            (self.repo / ".gitignore").read_text(encoding="utf-8").splitlines(),
            [".josi/"],
        )

    def test_init_preserves_gitignore_bytes_and_crlf(self):
        before = b"# Windows project\r\nbuild/\r\n"
        (self.repo / ".gitignore").write_bytes(before)
        self.initialize()
        self.assertEqual(
            (self.repo / ".gitignore").read_bytes(), before + b".josi/\n"
        )

    @unittest.skipIf(os.name == "nt", "trailing spaces in directory names are not portable")
    def test_init_preserves_repository_names_with_trailing_space(self):
        spaced = self.base / "repo "
        spaced.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(spaced)], check=True, capture_output=True
        )
        result = self.run_josi("init", cwd=spaced)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((spaced / ".josi" / "project.json").is_file())
        self.assertFalse(
            (self.repo / ".josi").exists(),
            "init must not write into the sibling directory 'repo'",
        )

    def test_init_gitignore_with_non_utf8_bytes(self):
        before = b"\xff\xfe legacy\n"
        (self.repo / ".gitignore").write_bytes(before)
        self.initialize()
        self.assertEqual(
            (self.repo / ".gitignore").read_bytes(), before + b".josi/\n"
        )

    def test_init_reports_metadata_write_failure(self):
        self.init_git()
        (self.repo / ".josi").write_text("not a directory", encoding="utf-8")
        result = self.run_josi("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".josi", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_init_reports_gitignore_read_failure(self):
        self.init_git()
        (self.repo / ".gitignore").mkdir()
        result = self.run_josi("init")
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
        result = self.run_josi("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OpenCode is not installed", result.stderr)
        self.assertFalse((self.repo / ".josi").exists())


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlanTests(JosiTestCase):
    def setUp(self):
        super().setUp()
        self.initialize()

    def test_first_planning_session(self):
        self.env["JOSI_FAKE_SESSION_ID"] = "ses_faketest0000000000000001"
        result = self.run_josi("plan")
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
        self.assertFalse(self.planner_path().exists(), "no session state is stored")
        self.assertEqual(self.switch_calls(), [], "fresh sessions already use the planner agent")

    def test_plan_installs_read_only_planner_agent(self):
        self.assertEqual(self.run_josi("plan").returncode, 0)
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
        self.assertEqual(self.run_josi("plan").returncode, 0)
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
            "josi task create *",
            "josi task list *",
            "josi task show *",
            "josi task update *",
            "josi task delete *",
        }
        for rule in rules:
            if rule["action"] == "shell" and rule["effect"] == "allow":
                self.assertIn(rule["resource"], management)
            if rule["action"] == "shell" and rule["effect"] == "deny":
                self.assertIn(rule["resource"], ("josi task *", "josi run *", "*>*"))
        # The .env asks must follow the broad read allow, otherwise the
        # last-match-wins rule would silently turn them into plain allows.
        broad_read = rules.index({"action": "read", "resource": "*", "effect": "allow"})
        env_ask = rules.index({"action": "read", "resource": "*.env", "effect": "ask"})
        self.assertLess(broad_read, env_ask)

    def test_plan_agent_preapproves_only_task_commands(self):
        self.assertEqual(self.run_josi("plan").returncode, 0)
        rules = self.planner_rules()
        # Only the task management subcommands are pre-approved, so the planner
        # can manage tasks without a prompt on every command...
        management_allows = [
            {"action": "shell", "resource": "josi task " + command + " *",
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
        task_deny = {"action": "shell", "resource": "josi task *", "effect": "deny"}
        self.assertIn(task_deny, rules)
        self.assertLess(rules.index(task_deny), rules.index(management_allows[0]))
        # Integrating a run into the base branch is never the planner's job.
        run_deny = {"action": "shell", "resource": "josi run *", "effect": "deny"}
        self.assertIn(run_deny, rules)
        self.assertLess(rules.index(task_deny), rules.index(run_deny))
        # ...while the rules stay effective: last match wins, so the broad ask
        # must come before the task deny, and the redirect deny (which blocks
        # "josi task ... > file" silently overwriting files) must come after
        # the task allows. The catch-all deny comes first.
        self.assertLess(rules.index(ask), rules.index(task_deny))
        for shell_allow in management_allows:
            self.assertLess(rules.index(shell_allow), rules.index(redirect_deny))
        content = self.agent_path().read_text(encoding="utf-8")
        self.assertIn("josi task create", content)
        self.assertIn("checked separately", content)
        self.assertIn("never open .josi/tasks.db", content)

    def test_plan_installs_planner_agent_once(self):
        first = self.run_josi("plan")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("Updated the planner agent", first.stdout)
        mtime = self.agent_path().stat().st_mtime_ns

        second = self.run_josi("plan")
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

        result = self.run_josi("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the planner agent", result.stdout)
        content = self.agent_path().read_text(encoding="utf-8")
        self.assertNotIn("old planner body", content)
        self.assertNotIn("git status", content, "stale pre-approvals must be removed")
        self.assertEqual(content, PLANNER_AGENT_DOC)

    def test_every_plan_run_starts_a_fresh_session(self):
        self.env["JOSI_FAKE_SESSION_ID"] = "ses_faketest0000000000000002"
        self.assertEqual(self.run_josi("plan").returncode, 0)
        self.env["JOSI_FAKE_SESSION_ID"] = "ses_faketest0000000000000003"
        result = self.run_josi("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Started planner session", result.stdout)
        self.assertNotIn("Resuming", result.stdout)

        creates = self.create_calls()
        self.assertEqual(len(creates), 2, "every run creates a session")
        self.assertFalse(any(
            c["argv"][:2] == ["api", "GET"] for c in self.calls()
        ), "no earlier session is ever looked up")
        argv = self.tui_calls()[1]["argv"]
        self.assertEqual(
            argv[argv.index("--session") + 1], "ses_faketest0000000000000003"
        )
        self.assertFalse(self.planner_path().exists())
        # previous sessions are preserved: nothing is ever deleted
        self.assertFalse(any("delete" in c["argv"] for c in self.calls()))

    def test_plan_no_longer_accepts_new(self):
        result = self.run_josi("plan", "--new")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_plan_never_sends_an_initial_prompt(self):
        self.env["JOSI_FAKE_SESSION_ID"] = "ses_faketest0000000000000011"
        self.assertEqual(self.run_josi("plan").returncode, 0)
        self.assertEqual(self.run_josi("plan").returncode, 0)
        self.env["JOSI_FAKE_SESSION_ID"] = "ses_faketest0000000000000012"
        self.assertEqual(self.run_josi("plan").returncode, 0)
        self.assertEqual(len(self.tui_calls()), 3)
        for call in self.tui_calls():
            self.assertNotIn("--prompt", call["argv"])

    def test_plan_reports_agent_switch_failure(self):
        self.env["JOSI_FAKE_SESSION_ID"] = "ses_faketest0000000000000013"
        self.env["JOSI_FAKE_CREATE_AGENT"] = "build"
        self.env["JOSI_FAKE_SWITCH_EXIT"] = "1"
        result = self.run_josi("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot select the planner agent", result.stderr)
        self.assertEqual(self.tui_calls(), [], "OpenCode must not be launched")

    def test_plan_agent_install_failure_is_reported(self):
        self.agent_path().mkdir(parents=True)  # a directory cannot be overwritten
        result = self.run_josi("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not write the planner agent", result.stderr)
        self.assertEqual(self.tui_calls(), [])

    def test_plan_from_subdirectory_uses_project_root(self):
        sub = self.repo / "src" / "nested"
        sub.mkdir(parents=True)
        self.env["JOSI_FAKE_SESSION_ID"] = "ses_faketest0000000000000008"
        result = self.run_josi("plan", cwd=sub)
        self.assertEqual(result.returncode, 0, result.stderr)
        tui = self.tui_calls()[0]
        self.assertEqual(tui["argv"][0], str(self.repo))
        self.assertEqual(tui["cwd"], str(self.repo))

    @unittest.skipIf(os.name == "nt", "trailing spaces in directory names are not portable")
    def test_plan_does_not_resume_a_sibling_repository(self):
        self.env["JOSI_FAKE_SESSION_ID"] = "ses_faketest0000000000000015"
        self.assertEqual(self.run_josi("plan").returncode, 0)
        self.assertEqual(len(self.tui_calls()), 1)

        spaced = self.base / "repo "
        spaced.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(spaced)], check=True, capture_output=True
        )
        result = self.run_josi("plan", cwd=spaced)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("josi init", result.stderr)
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
        # selected executable must keep working after josi switches its
        # subprocesses to the project root.
        self.env["PATH"] = "." + os.pathsep + MINIMAL_PATH
        result = self.run_josi("plan", cwd=nested)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Started planner session", result.stdout)
        self.assertEqual(len(self.tui_calls()), 1)

    def test_plan_does_not_cross_a_nested_git_repository(self):
        child = self.repo / "child-repo"
        child.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(child)], check=True, capture_output=True
        )
        result = self.run_josi("plan", cwd=child)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("josi init", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((child / ".opencode").exists())

    def test_plan_fails_when_opencode_is_missing(self):
        if shutil.which("opencode", path=MINIMAL_PATH):
            self.skipTest("a real opencode executable is visible on the minimal PATH")
        self.env["PATH"] = MINIMAL_PATH
        result = self.run_josi("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OpenCode is not installed", result.stderr)
        self.assertEqual(self.tui_calls(), [])

    def test_plan_session_creation_failure(self):
        self.env["JOSI_FAKE_CREATE_EXIT"] = "1"
        result = self.run_josi("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not create", result.stderr)
        self.assertFalse(self.planner_path().exists())
        self.assertEqual(self.tui_calls(), [])

    def test_plan_session_created_in_wrong_directory_is_rejected(self):
        self.env["JOSI_FAKE_CREATE_DIR"] = str(self.base / "elsewhere")
        result = self.run_josi("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("belongs to", result.stderr)
        self.assertFalse(self.planner_path().exists())
        self.assertEqual(self.tui_calls(), [])

    def test_plan_tui_failure_is_reported(self):
        self.env["JOSI_FAKE_SESSION_ID"] = "ses_faketest0000000000000005"
        self.env["JOSI_FAKE_TUI_EXIT"] = "7"
        result = self.run_josi("plan")
        self.assertEqual(result.returncode, 7)
        self.assertIn("exited with status 7", result.stderr)

    def test_plan_rejects_malformed_session_location(self):
        self.env["JOSI_FAKE_CREATE_LOCATION"] = json.dumps({"directory": ["bad"]})
        result = self.run_josi("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected session directory", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.planner_path().exists())
        self.assertEqual(self.tui_calls(), [])

    def test_plan_switches_agent_on_new_session_when_needed(self):
        self.env["JOSI_FAKE_SESSION_ID"] = "ses_faketest0000000000000014"
        self.env["JOSI_FAKE_CREATE_AGENT"] = "build"
        result = self.run_josi("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        switches = self.switch_calls()
        self.assertEqual(len(switches), 1)
        argv = switches[0]["argv"]
        self.assertEqual(argv[2], "/api/session/ses_faketest0000000000000014/agent")
        self.assertEqual(
            json.loads(argv[argv.index("--data") + 1]), {"agent": PLANNER_AGENT_NAME}
        )
        self.assertEqual(len(self.tui_calls()), 1)


@unittest.skipUnless(shutil.which("git"), "git is required")
class PlanGuardTests(JosiTestCase):
    def test_plan_before_init_does_not_launch_opencode(self):
        self.init_git()
        result = self.run_josi("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("josi init", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.repo / ".opencode").exists())

    def test_plan_with_missing_metadata(self):
        self.init_git()
        (self.repo / ".josi").mkdir()
        result = self.run_josi("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Josi project metadata", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.repo / ".opencode").exists())

    def test_plan_with_invalid_metadata(self):
        self.init_git()
        (self.repo / ".josi").mkdir()
        payloads = [
            "not json at all",
            json.dumps([]),
            json.dumps({}),
            json.dumps({"schema_version": 99, "name": "repo"}),
            json.dumps({"schema_version": 1, "name": 42}),
            json.dumps({"schema_version": 1, "name": "repo", "base_branch": 42}),
            json.dumps({"schema_version": 1, "name": "repo", "base_branch": "  "}),
            json.dumps({"schema_version": 1, "name": "repo", "agent": "codex"}),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.metadata_path().write_text(payload, encoding="utf-8")
                result = self.run_josi("plan")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid Josi project metadata", result.stderr)
                self.assertEqual(self.calls(), [])
                self.assertFalse((self.repo / ".opencode").exists())

    def test_plan_with_undecodable_metadata(self):
        self.init_git()
        (self.repo / ".josi").mkdir()
        (self.repo / ".josi" / "project.json").write_bytes(b"\xff\xfe")
        result = self.run_josi("plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Josi project metadata", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_plan_outside_git_repository(self):
        outside = self.base / "outside"
        outside.mkdir()
        result = self.run_josi("plan", cwd=outside)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git repository", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_plan_ignores_josi_metadata_outside_git(self):
        outside = self.base / "copied"
        (outside / ".josi").mkdir(parents=True)
        (outside / ".josi" / "project.json").write_text(
            json.dumps({"schema_version": 1, "name": "copied", "created_at": "x"}),
            encoding="utf-8",
        )
        result = self.run_josi("plan", cwd=outside)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git repository", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((outside / ".opencode").exists())


if __name__ == "__main__":
    unittest.main()
