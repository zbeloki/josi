"""Minimal wrapper around the OpenCode CLI.

actl delegates everything session-related to OpenCode: OpenCode owns the
session store, the conversation transcripts, and the interactive TUI. actl
only creates a project-scoped session, verifies where an existing session
lives, and launches the TUI against it.

The integration targets the OpenCode v2 CLI (tested with 2.1.2):

* ``opencode api POST /api/session --data {...}`` creates a session at an
  explicit location (``{"location": {"directory": "<project root>"}}``) using
  a given agent and returns the generated session ID. Creating the session
  explicitly is necessary because the TUI creates sessions lazily and would
  otherwise generate an ID actl cannot capture.
* ``opencode api GET /api/session/<id>`` returns session metadata, including
  the directory the session belongs to and the selected agent, so actl can
  refuse to resume a session that does not belong to the project and can
  re-select the planner agent on sessions created before it existed.
* ``opencode api POST /api/session/<id>/agent --data {"agent": ...}`` switches
  the agent used by subsequent turns of an existing session.
* ``opencode <directory> --session <id>`` opens the interactive TUI, resuming
  the given session and waiting for user input.
* ``opencode run --agent <agent> --model <provider/model> --session <id>
  "<message>"`` runs one noninteractive turn: OpenCode executes the message
  with the given agent and model, prints the transcript to stdout/stderr, and
  exits with status 0 when the turn succeeded. Actl uses this to run the
  worker (the built-in ``build`` agent) inside a task worktree; a permission
  that would ask the user in a TUI is auto-rejected in a noninteractive run,
  so the worker never blocks on a prompt.
* ``opencode run ... --format json`` prints the turn as JSONL events instead
  of a rendered transcript. actl uses this for the reviewer (the built-in
  read-only ``plan`` agent), so the final message - the structured verdict -
  can be read back without scraping the rendered output.

OpenCode processes inherit the normal process environment; an explicitly
supplied environment is passed through unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

BINARY = "opencode"

# The built-in agent used for noninteractive task execution: it may read,
# write, and run shell commands without asking (except for explicit denials
# such as reading .env files or reaching outside the project directory, which
# a noninteractive run rejects instead of prompting).
WORKER_AGENT = "build"

# The built-in read-only agent used for the reviewer: editing and browser
# tools are denied, so a review cannot change even its own disposable
# checkout. Shell inspection commands stay available.
REVIEWER_AGENT = "plan"


class OpenCodeError(RuntimeError):
    """Raised when OpenCode is unavailable, fails, or returns bad data."""


@dataclass(frozen=True)
class Session:
    """A session as reported by OpenCode."""

    id: str
    directory: str
    agent: str = ""


def find_binary() -> str:
    """Return the absolute path of the OpenCode executable, or raise OpenCodeError.

    The result is absolute so that later subprocesses, which run with a
    different working directory, still execute the same binary that was found
    here (``shutil.which`` may return a relative path when ``PATH`` contains a
    relative entry such as ``.``).
    """
    path = shutil.which(BINARY)
    if path is None:
        raise OpenCodeError(
            "OpenCode is not installed or not on PATH (no 'opencode' executable found)"
        )
    return os.path.abspath(path)


def create_session(binary: str, project_root: Path, title: str, agent: str) -> Session:
    """Create a new empty session at ``project_root``, bound to ``agent``."""
    root = _realpath(project_root)
    payload = _api(
        binary,
        project_root,
        "POST",
        "/api/session",
        {"title": title, "agent": agent, "location": {"directory": root}},
    )
    session = _parse_session(payload, "creating an OpenCode session")
    _require_location(session, root)
    return session


def get_session(binary: str, project_root: Path, session_id: str) -> Session:
    """Return session metadata, verifying that it belongs to ``project_root``."""
    root = _realpath(project_root)
    payload = _api(binary, project_root, "GET", "/api/session/" + session_id)
    session = _parse_session(payload, "loading OpenCode session " + repr(session_id))
    if session.id != session_id:
        raise OpenCodeError(
            "OpenCode returned session " + repr(session.id)
            + " instead of the requested " + repr(session_id)
        )
    _require_location(session, root)
    return session


def session_usage(binary: str, directory: Path, session_id: str, env=None) -> dict:
    """Return the raw session metadata used to capture one invocation's usage.

    ``GET /api/session/<id>`` reports the session's cumulative ``cost`` and
    ``tokens``; the caller turns the cumulative totals into per-invocation
    usage. A response without a usable ``data`` object raises
    :class:`OpenCodeError`.
    """
    payload = _api(
        binary, directory, "GET", "/api/session/" + session_id, env=env
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise OpenCodeError(
            "unexpected OpenCode response while reading the usage of session "
            + repr(session_id)
        )
    return data


def model_list(binary: str, directory: Path, env=None) -> list:
    """Return the models OpenCode knows about, with their pricing metadata.

    ``GET /api/model`` reports every available model; actl reads the cost of
    the model it launched to derive input/output costs when OpenCode did not
    report a cost itself.
    """
    payload = _api(binary, directory, "GET", "/api/model", env=env)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise OpenCodeError("unexpected OpenCode response while listing models")
    return data


def switch_agent(binary: str, project_root: Path, session_id: str, agent: str) -> None:
    """Select the agent used by subsequent turns of an existing session."""
    _api(
        binary,
        project_root,
        "POST",
        "/api/session/" + session_id + "/agent",
        {"agent": agent},
        expect_json=False,
    )


def launch_tui(binary: str, project_root: Path, session_id: str, env=None) -> int:
    """Launch the interactive OpenCode TUI on ``session_id`` and return its exit code.

    ``env`` is the process environment; when omitted, the current process
    environment is inherited.
    """
    command = [binary, str(project_root), "--session", session_id]
    try:
        return subprocess.run(
            command, cwd=str(project_root), env=env
        ).returncode
    except OSError as exc:
        raise OpenCodeError("could not launch OpenCode: " + str(exc)) from exc


def run_headless(
    binary: str,
    directory: Path,
    session_id: str,
    model: str,
    prompt: str,
    output,
    pass_fds=(),
    agent: str = WORKER_AGENT,
    output_format: str = None,
    env=None,
) -> int:
    """Run one noninteractive OpenCode turn in ``directory``.

    The transcript is streamed into ``output`` (a writable file object); the
    turn's stdout and stderr are merged so the log keeps the original order.
    ``pass_fds`` are kept open in the OpenCode process - actl passes the
    execution lock file descriptor so the lock outlives an actl crash.
    ``agent`` selects the OpenCode agent; ``output_format`` selects the CLI
    output format (``"json"`` makes the reviewer's final message machine
    readable). ``env`` is the process environment; when omitted, the
    current process environment is inherited. Returns OpenCode's exit status.
    """
    command = [
        binary, "run",
        "--agent", agent,
        "--model", model,
        "--session", session_id,
    ]
    if output_format is not None:
        command += ["--format", output_format]
    command.append(prompt)
    try:
        proc = subprocess.run(
            command,
            cwd=str(directory),
            stdout=output,
            stderr=subprocess.STDOUT,
            pass_fds=tuple(pass_fds),
            env=env,
        )
    except OSError as exc:
        raise OpenCodeError("could not run OpenCode: " + str(exc)) from exc
    return proc.returncode


def _realpath(path: Path) -> str:
    return os.path.realpath(str(path))


def _api(
    binary: str, cwd: Path, method: str, path: str, body=None,
    expect_json=True, env=None,
):
    command = [binary, "api", method, path]
    if body is not None:
        command += ["--data", json.dumps(body)]
    try:
        proc = subprocess.run(
            command, cwd=str(cwd), capture_output=True, text=True, env=env
        )
    except OSError as exc:
        raise OpenCodeError("could not run OpenCode: " + str(exc)) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise OpenCodeError(
            "OpenCode request " + method + " " + path + " failed: " + detail
        )
    if not expect_json:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        output = proc.stdout.strip()
        if len(output) > 200:
            output = output[:200] + "..."
        raise OpenCodeError(
            "OpenCode returned unexpected output for "
            + method + " " + path + ": " + repr(output)
        ) from exc


def _parse_session(payload, context: str) -> Session:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise OpenCodeError("unexpected OpenCode response while " + context)
    session_id = data.get("id")
    if not isinstance(session_id, str) or not session_id.startswith("ses"):
        raise OpenCodeError(
            "unexpected OpenCode session id while " + context + ": " + repr(session_id)
        )
    location = data.get("location")
    directory = location.get("directory") if isinstance(location, dict) else None
    if directory is not None and not isinstance(directory, str):
        raise OpenCodeError(
            "unexpected session directory while " + context + ": " + repr(directory)
        )
    agent = data.get("agent")
    if agent is not None and not isinstance(agent, str):
        raise OpenCodeError(
            "unexpected session agent while " + context + ": " + repr(agent)
        )
    return Session(id=session_id, directory=directory or "", agent=agent or "")


def _require_location(session: Session, expected_root: str) -> None:
    actual = os.path.realpath(session.directory) if session.directory else ""
    if actual != expected_root:
        where = repr(session.directory) if session.directory else "an unknown location"
        raise OpenCodeError(
            "session " + repr(session.id) + " belongs to "
            + where + ", not to " + repr(expected_root)
        )
