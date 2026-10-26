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

Every launch - the planner TUI, the worker, the reviewer, and the escalation
worker - also receives a launch-time OpenCode configuration through the
``OPENCODE_CONFIG_CONTENT`` environment variable. actl defaults OpenRouter
requests to provider-level price sorting without naming a provider or a model;
the default is layered *below* any configuration the project already provides,
so a project or model-level override wins and unrelated settings survive. See
:func:`launch_environment`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

BINARY = "opencode"

# OpenCode merges the JSON passed through this environment variable over every
# configuration file it discovers, so actl can apply a launch-time default
# without writing to (or rewriting) the project's OpenCode files.
OPENCODE_CONFIG_CONTENT = "OPENCODE_CONFIG_CONTENT"

# The OpenCode configuration file actl reads when a project or the user
# already selected a custom config path.
OPENCODE_CONFIG = "OPENCODE_CONFIG"

# Config file names OpenCode discovers in a directory and its ``.opencode``
# subdirectory, in the order they are merged (a later name wins).
CONFIG_FILENAMES = ("opencode.json", "opencode.jsonc")

# The launch-time default actl applies to every OpenCode process it starts.
#
# OpenRouter accepts a provider-routing object in the request body; sorting
# its providers by price routes every OpenRouter model to the cheapest
# eligible provider. The default is set on the ``openrouter`` provider (not on
# any model), so it covers every OpenRouter model actl launches and actl never
# has to know or pick a model or provider. Only the OpenRouter provider is
# touched, so unrelated providers, models, and settings are left alone.
DEFAULT_LAUNCH_CONFIG = {
    "provider": {
        "openrouter": {
            "body": {
                "provider": {"sort": "price"},
            },
        },
    },
}

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


def launch_environment(directory, base_env=None) -> dict:
    """Return the environment for an OpenCode launch in ``directory``.

    The returned mapping is ``base_env`` (the process environment by default)
    with ``OPENCODE_CONFIG_CONTENT`` set to actl's launch-time configuration:
    :data:`DEFAULT_LAUNCH_CONFIG` layered *below* whatever configuration the
    project already provides. The default therefore only fills in settings the
    project did not specify, so a project or model-level override wins and
    unrelated configuration survives.

    The existing configuration is read the same way OpenCode reads it, from
    lowest to highest precedence: the custom config file named by
    ``OPENCODE_CONFIG``, then ``opencode.json(c)`` and
    ``.opencode/opencode.json(c)`` discovered from the filesystem root down to
    ``directory`` (direct files first, then ``.opencode`` files), and finally
    any configuration the caller already passed through
    ``OPENCODE_CONFIG_CONTENT``.
    """
    env = dict(os.environ if base_env is None else base_env)
    config = _deep_merge(DEFAULT_LAUNCH_CONFIG, existing_launch_config(directory, env))
    env[OPENCODE_CONFIG_CONTENT] = json.dumps(config, sort_keys=True)
    return env


def existing_launch_config(directory, env) -> dict:
    """Return the project's OpenCode configuration for ``directory``.

    Only existing configuration is returned; the actl default is applied by
    :func:`launch_environment`. A missing, unreadable, or malformed file
    contributes nothing instead of failing the launch.
    """
    config = {}
    custom = env.get(OPENCODE_CONFIG)
    if custom:
        path = Path(custom)
        if not path.is_absolute():
            path = Path(directory) / path
        config = _deep_merge(config, _read_config_file(path))
    config = _deep_merge(config, _discovered_config(directory))
    config = _deep_merge(
        config, _parse_config(env.get(OPENCODE_CONFIG_CONTENT))
    )
    return config


def _discovered_config(directory) -> dict:
    """Merge the OpenCode config files discovered above ``directory``.

    Files are merged from the farthest ancestor to ``directory`` in the same
    order OpenCode uses: every direct ``opencode.json(c)`` first, then every
    ``.opencode/opencode.json(c)`` (so a ``.opencode`` file overrides a direct
    one). Files that do not exist are skipped.
    """
    directory = Path(directory).resolve()
    chain = []
    current = directory
    while True:
        chain.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    chain.reverse()
    config = {}
    for folder in chain:
        for name in CONFIG_FILENAMES:
            config = _deep_merge(config, _read_config_file(folder / name))
    for folder in chain:
        for name in CONFIG_FILENAMES:
            config = _deep_merge(
                config, _read_config_file(folder / ".opencode" / name)
            )
    return config


def _read_config_file(path) -> dict:
    """Return the JSON object in ``path``, or an empty object on any error."""
    if path is None:
        return {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return _parse_config(text)


def _parse_config(text) -> dict:
    """Parse JSON or JSONC text into a dict; return ``{}`` on any error."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = json.loads(_strip_jsonc(text))
        except (TypeError, ValueError):
            return {}
    return data if isinstance(data, dict) else {}


def _strip_jsonc(text: str) -> str:
    """Remove JSONC comments and trailing commas so ``json`` can parse it.

    Strings (including escaped quotes) are copied verbatim, so a ``//`` or
    ``/*`` inside a string value is never treated as a comment and a comma
    inside a string never looks like a trailing comma.
    """
    result = []
    index = 0
    length = len(text)
    in_string = False
    escape = False
    while index < length:
        char = text[index]
        if in_string:
            result.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (
                text[index] == "*" and text[index + 1] == "/"
            ):
                index += 1
            index += 2
            continue
        if char == ",":
            following = _skip_jsonc_trivia(text, index + 1)
            if following < length and text[following] in "}]":
                index += 1
                continue
        result.append(char)
        index += 1
    return "".join(result)


def _skip_jsonc_trivia(text: str, index: int) -> int:
    """Return the index of the next JSON token, skipping whitespace/comments."""
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
        elif char == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
        elif char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (
                text[index] == "*" and text[index + 1] == "/"
            ):
                index += 1
            index += 2
        else:
            break
    return index


def _deep_merge(base: dict, override: dict) -> dict:
    """Return ``base`` with ``override`` merged over it.

    Matching objects are merged key by key; any other value (including a
    list) is replaced by the override, so the more specific configuration
    wins and unrelated keys survive.
    """
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = value
    return result


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

    ``env`` is the process environment; when omitted, actl's launch-time
    OpenCode configuration is applied (see :func:`launch_environment`).
    """
    command = [binary, str(project_root), "--session", session_id]
    if env is None:
        env = launch_environment(project_root)
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
    readable). ``env`` is the process environment; when omitted, actl's
    launch-time OpenCode configuration is applied (see
    :func:`launch_environment`). Returns OpenCode's exit status.
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
    if env is None:
        env = launch_environment(directory)
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
    if env is None:
        env = launch_environment(cwd)
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
