"""Agent backends: the CLIs josi drives to plan, implement, and review.

A project selects one backend (``agent`` in ``.josi/project.json``) and every
role - planner, worker, reviewer, escalation worker, test writer - runs on it.
Two backends exist:

* :class:`OpenCodeBackend` wraps the OpenCode CLI (see :mod:`josi.opencode`).
  It creates sessions explicitly, resumes them by ID, and reads usage from
  the session metadata.
* :class:`ClaudeBackend` drives the Claude Code CLI (``claude``). A session is
  a UUID chosen by josi: the first turn passes ``--session-id`` and later turns
  of the same session pass ``--resume``. Noninteractive turns run as
  ``claude -p --output-format stream-json --verbose`` with the prompt on stdin
  and ``--permission-mode bypassPermissions`` (the isolated worktree is the
  boundary); the stream's final ``result`` event carries the answer text, the
  token counts, and the cost of that one turn.

Model settings are ``(name, effort)`` pairs. In josi's own code they travel as
one ``name#effort`` string, which OpenCode takes as ``provider/model#variant``
and which the Claude backend splits into ``--model`` and ``--effort``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from . import opencode, usage

AGENT_OPENCODE = "opencode"
AGENT_CLAUDE = "claude"
AGENTS = (AGENT_OPENCODE, AGENT_CLAUDE)
DEFAULT_AGENT = AGENT_OPENCODE

ROLE_PLANNER = "planner"
ROLE_WORKER = "worker"
ROLE_REVIEWER = "reviewer"

# Model settings every project carries, with each backend's defaults as
# ``(model, effort)`` pairs.
MODEL_KEYS = (
    "worker_model", "reviewer_model", "escalation_worker_model",
    "test_writer_model",
)
DEFAULT_MODELS = {
    AGENT_OPENCODE: {
        "worker_model": ("openrouter/deepseek/deepseek-v4.1-flash", "low"),
        "reviewer_model": ("openrouter/openai/gpt-6-luna", "high"),
        "escalation_worker_model": (
            "openrouter/anthropic/claude-sonnet-5.5", "medium"
        ),
        "test_writer_model": ("openrouter/z-ai/glm-5.3", "high"),
    },
    AGENT_CLAUDE: {
        "worker_model": ("sonnet", "low"),
        "reviewer_model": ("opus", "medium"),
        "escalation_worker_model": ("opus", "medium"),
        "test_writer_model": ("sonnet", "medium"),
    },
}

CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Tools the Claude planner may never use. Editing, web access, subagents, and
# the task-execution commands stay out of reach; MCP servers are not loaded
# (``--strict-mcp-config`` without a config).
CLAUDE_PLANNER_DISALLOWED = (
    "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
    "Task", "Agent",
    "Bash(josi task run:*)", "Bash(josi task reset:*)", "Bash(josi run:*)",
)
CLAUDE_PLANNER_ALLOWED = (
    "Read", "Glob", "Grep",
    "Bash(josi task create:*)", "Bash(josi task list:*)",
    "Bash(josi task show:*)", "Bash(josi task update:*)",
    "Bash(josi task delete:*)",
)
# The reviewer must not change its checkout or reach the web.
CLAUDE_REVIEWER_DISALLOWED = (
    "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
)

AgentError = opencode.OpenCodeError


class ClaudeError(AgentError):
    """Raised when Claude Code is unavailable or fails."""


def split_model(setting: str):
    """Split ``name#effort`` into ``(name, effort)``; effort is ``""`` if absent."""
    name, _, effort = setting.partition("#")
    return name, effort


def join_model(name: str, effort) -> str:
    return name + "#" + effort if effort else name


def default_model(agent: str, key: str) -> str:
    return join_model(*DEFAULT_MODELS[agent][key])


def parse_model(agent: str, value: str, role: str) -> str:
    """Validate a ``name#effort`` model setting for ``agent``.

    Raises :class:`ValueError` with a user-facing message. OpenCode takes
    ``provider/model`` with an optional ``#variant``; Claude Code takes a model
    name or alias (``sonnet``, ``opus``, ``claude-sonnet-5-5``) with an
    optional ``#effort``.
    """
    setting = value.strip()
    name, effort = split_model(setting)
    if agent == AGENT_CLAUDE:
        if (
            not name or name.startswith("-")
            or any(char.isspace() for char in setting)
            or (effort and effort not in CLAUDE_EFFORTS)
            or ("#" in setting and not effort)
        ):
            raise ValueError(
                "invalid " + role + " model " + repr(value)
                + " (expected a Claude model such as sonnet or opus,"
                " optionally #" + "|".join(CLAUDE_EFFORTS) + ")"
            )
        return setting
    provider, _, rest = setting.partition("/")
    if (
        not provider or not rest
        or setting.startswith("-")
        or any(char.isspace() for char in setting)
    ):
        raise ValueError(
            "invalid " + role + " model " + repr(value)
            + " (expected provider/model, optionally provider/model#variant)"
        )
    return setting


def find_binary(agent: str) -> str:
    """Return the absolute path of ``agent``'s executable or raise AgentError."""
    if agent == AGENT_CLAUDE:
        return ClaudeBackend.find_binary()
    return opencode.find_binary()


def create(agent: str):
    """Return the backend for ``agent`` bound to its installed executable."""
    if agent == AGENT_CLAUDE:
        return ClaudeBackend(find_binary(agent))
    return OpenCodeBackend(find_binary(agent))


class OpenCodeBackend:
    name = AGENT_OPENCODE
    label = "OpenCode"

    def __init__(self, binary: str):
        self.binary = binary
        # OpenCode's model list carries the pricing metadata costs are
        # derived from. It does not change during one josi process.
        self._models = None

    def create_session(self, directory: Path, title: str, role: str):
        agent = (
            opencode.REVIEWER_AGENT if role == ROLE_REVIEWER
            else opencode.WORKER_AGENT
        )
        return opencode.create_session(
            self.binary, directory, title=title, agent=agent
        )

    def run_headless(
        self, directory, session, model, prompt, output, role,
        pass_fds=(), structured=False,
    ) -> int:
        return opencode.run_headless(
            self.binary, directory, session.id, model, prompt, output,
            pass_fds=pass_fds,
            agent=(
                opencode.REVIEWER_AGENT if role == ROLE_REVIEWER
                else opencode.WORKER_AGENT
            ),
            output_format="json" if structured else None,
        )

    def final_text(self, output: str) -> str:
        """Return the final assistant message of ``--format json`` output."""
        message = None
        parts = []
        for event in _events(output):
            if event.get("type") != "text":
                continue
            part = event.get("part")
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                continue
            if part.get("messageID") != message:
                message = part.get("messageID")
                parts = []
            parts.append(part["text"])
        return "".join(parts)

    def turn_usage(self, directory, session, model, turn_output, previous):
        """Return ``(cumulative, record)`` for the turn that just ended.

        OpenCode reports the session's cumulative usage; the previous
        cumulative value is subtracted so each turn is recorded on its own.
        Raises :class:`AgentError` when the usage cannot be read.
        """
        current = usage.parse_session(
            opencode.session_usage(self.binary, directory, session.id)
        )
        record = usage.derive(
            usage.subtract(current, previous),
            self._pricing(directory, model),
        )
        return current, record

    def _pricing(self, directory, model):
        if self._models is None:
            try:
                self._models = opencode.model_list(self.binary, directory)
            except (AgentError, OSError):
                self._models = []
        return usage.pricing_from_models(model, self._models)


class ClaudeBackend:
    name = AGENT_CLAUDE
    label = "Claude Code"
    BINARY = "claude"

    def __init__(self, binary: str):
        self.binary = binary
        self._started = set()

    @classmethod
    def find_binary(cls) -> str:
        path = shutil.which(cls.BINARY)
        if path is None:
            raise ClaudeError(
                "Claude Code is not installed or not on PATH"
                " (no 'claude' executable found)"
            )
        return os.path.abspath(path)

    def create_session(self, directory: Path, title: str, role: str):
        return opencode.Session(id=str(uuid.uuid4()), directory=str(directory))

    def run_headless(
        self, directory, session, model, prompt, output, role,
        pass_fds=(), structured=False,
    ) -> int:
        name, effort = split_model(model)
        command = [self.binary, "-p", "--model", name]
        if effort:
            command += ["--effort", effort]
        if session.id in self._started:
            command += ["--resume", session.id]
        else:
            command += ["--session-id", session.id]
        command += [
            "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json", "--verbose",
        ]
        if role == ROLE_REVIEWER:
            command += ["--disallowedTools", ",".join(CLAUDE_REVIEWER_DISALLOWED)]
        self._started.add(session.id)
        try:
            proc = subprocess.run(
                command,
                cwd=str(directory),
                input=prompt.encode("utf-8"),
                stdout=output,
                stderr=subprocess.STDOUT,
                pass_fds=tuple(pass_fds),
            )
        except OSError as exc:
            raise ClaudeError("could not run Claude Code: " + str(exc)) from exc
        return proc.returncode

    def final_text(self, output: str) -> str:
        """Return the ``result`` text of the turn's final ``result`` event."""
        result = _last_result(output)
        text = result.get("result") if result else None
        if not isinstance(text, str) or result.get("is_error"):
            return ""
        return text

    def turn_usage(self, directory, session, model, turn_output, previous):
        """Return ``(None, record)`` from the turn's ``result`` event.

        ``claude -p`` reports the usage and cost of that invocation alone, so
        there is no cumulative value to carry between turns.
        """
        result = _last_result(turn_output)
        if result is None:
            return None, usage.unavailable()
        tokens = result.get("usage")
        cost = result.get("total_cost_usd")
        if not isinstance(tokens, dict) and cost is None:
            return None, usage.unavailable()
        return None, usage.Usage(
            available=True,
            input_tokens=usage._token(tokens, "input_tokens"),
            output_tokens=usage._token(tokens, "output_tokens"),
            cache_read_tokens=usage._token(tokens, "cache_read_input_tokens"),
            cache_write_tokens=usage._token(
                tokens, "cache_creation_input_tokens"
            ),
            cost_total=usage._as_float(cost),
            cost_source=usage.COST_REPORTED if cost is not None else None,
        )

    def planner_command(self, system_prompt: str) -> list:
        """Return the interactive planner command line."""
        return [
            self.binary,
            "--append-system-prompt", system_prompt,
            "--strict-mcp-config",
            "--allowedTools", ",".join(CLAUDE_PLANNER_ALLOWED),
            "--disallowedTools", ",".join(CLAUDE_PLANNER_DISALLOWED),
        ]

    def launch_planner(self, root: Path, system_prompt: str) -> int:
        """Run the interactive planner in a fresh session; return its exit code."""
        command = self.planner_command(system_prompt) + [
            "--session-id", str(uuid.uuid4()),
        ]
        try:
            return subprocess.run(command, cwd=str(root)).returncode
        except OSError as exc:
            raise ClaudeError("could not launch Claude Code: " + str(exc)) from exc


def _events(output: str):
    """Yield the JSON objects of JSONL ``output``, skipping other lines."""
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _last_result(output: str):
    result = None
    for event in _events(output):
        if event.get("type") == "result":
            result = event
    return result
