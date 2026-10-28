"""Test generation: the test-writer prompt and strict outcome parsing.

Before the first worker attempt, a noninteractive OpenCode test writer runs
in the task worktree with the project's test-writer model. It inspects the
project, decides whether the task needs automated tests, and either writes
and commits them or explicitly decides that no tests are needed and changes
nothing.

Like the reviewer, the test writer must end its turn with exactly one
complete JSON object; ``opencode run --format json`` makes the final message
machine-readable. Parsing is strict: prose around the answer, several
objects, truncated JSON, and unknown decisions are all rejected, so an
ambiguous answer can never pass as a decision. A message that is not one JSON
object at all may be reformatted once, like the reviewer's; an unknown or
otherwise unusable decision fails the stage directly. The orchestration layer
combines the parsed decision with the worktree state: ``tests_written``
counts only together with committed changes and a clean worktree, while
``no_tests_needed`` never requires a commit.
"""

from __future__ import annotations

import json

# The decision the test writer must end its turn with.
DECISION_TESTS_WRITTEN = "tests_written"
DECISION_NO_TESTS_NEEDED = "no_tests_needed"
DECISIONS = (DECISION_TESTS_WRITTEN, DECISION_NO_TESTS_NEEDED)


class TestGenError(Exception):
    """Raised when the test writer's outcome is missing or unusable."""


class TestGenFormatError(TestGenError):
    """The final message is not one JSON object and may be reformatted once."""


def build_prompt(task, branch: str, base_branch: str) -> str:
    """The message that asks the test writer to prepare the task's tests.

    The prompt carries the task title and description; the test writer works
    in the task worktree on the task branch and commits there, so the worker
    starts from the tests it left behind.
    """
    description = task.description.strip() or "(none)"
    return "\n".join([
        "Write the tests for this task in the current Git worktree"
        " (branch " + branch + ").",
        "",
        "Task t" + str(task.id) + ": " + task.title,
        "",
        description,
        "",
        "Instructions:",
        "- Inspect the relevant project files and follow the existing style.",
        "- First decide whether this task needs automated tests at all.",
        "- If it does, write or extend the project's tests for the task's",
        "  expected behavior and run them to verify that they pass.",
        "- If you explicitly decide that no tests are needed, change nothing",
        "  and say so in your final JSON answer instead.",
        "- Change only what the tests need, without touching unrelated",
        "  functionality.",
        "- Commit your test changes on the current branch with Git. When you",
        "  finish, the worktree must be clean: no uncommitted or untracked",
        "  files outside .gitignore.",
        "- Never merge into " + base_branch + ".",
        "",
        "When you finish, reply with exactly one JSON object and nothing"
        " else. Your final message is parsed by a machine: do not include a",
        "test summary, explanation, introductory prose, or Markdown before"
        " or after the JSON. Put explanations inside the JSON. Use one of",
        "these forms:",
        '{"decision": "tests_written", "summary": "..."}',
        '{"decision": "no_tests_needed", "reason": "..."}',
        "Use tests_written only when you committed test changes on the"
        " branch, and no_tests_needed only when you deliberately decided",
        "that this task needs no tests and changed nothing.",
    ])


def format_retry_prompt() -> str:
    """Ask the same test writer to reformat, not reconsider its decision."""
    return (
        "Your final message was rejected because it was not one complete"
        " JSON object. Re-emit your decision as exactly one JSON object, and"
        " nothing else.\n"
        "Preserve the decision you already made, with the same summary or"
        " reason. Do not change the decision, write or commit tests, run"
        " commands, or modify any file. Do not use tools. Do not include a"
        " test summary, introductory prose, Markdown fences, or any text"
        " outside the JSON."
    )


def outcome_from_output(output: str) -> str:
    """Extract the test writer's decision from its ``--format json`` output.

    ``output`` is the complete stdout/stderr of the turn: JSONL events with
    any non-JSON stderr lines mixed in.
    """
    text = _final_text(output)
    if not text.strip():
        raise TestGenError("the test writer produced no text output")
    return parse_outcome(text)


def parse_outcome(text: str) -> str:
    """Parse and validate one test-writer decision.

    The final message must be exactly one complete JSON object whose
    ``decision`` is one of :data:`DECISIONS`; a message that is not one JSON
    object - prose, several objects, truncated JSON - raises
    :class:`TestGenFormatError` (the orchestration layer may ask once for a
    reformat), while an unusable decision such as an unknown one raises a
    plain :class:`TestGenError`. Extra members such as a summary or a reason
    are ignored here; the full transcript stays in the stage log.
    """
    stripped = _strip_code_fence(text.strip())
    try:
        payload = json.loads(stripped)
    except ValueError:
        raise TestGenFormatError(
            "the test writer output is not one complete JSON object"
            " (nothing may appear before or after it)"
        )
    if not isinstance(payload, dict):
        raise TestGenFormatError(
            "the test writer outcome is not a JSON object"
        )
    decision = payload.get("decision")
    if decision not in DECISIONS:
        raise TestGenError("unknown test-writer decision " + repr(decision))
    return decision


def _final_text(output: str) -> str:
    """Return the text of the final assistant message in JSON event output.

    Text parts of the last assistant message are joined; earlier commentary
    and tool output are ignored. Non-JSON lines (stderr) are skipped. This
    mirrors the reviewer's parsing of the same OpenCode output format.
    """
    message = None
    parts = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part")
        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
            continue
        if part.get("messageID") != message:
            message = part.get("messageID")
            parts = []
        parts.append(part["text"])
    return "".join(parts)


def _strip_code_fence(text: str) -> str:
    """Remove one surrounding Markdown code fence, when present.

    Mirrors the reviewer's tolerance: a single surrounding fence is accepted,
    prose around the JSON object is not.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    newline = stripped.find("\n")
    if newline >= 0:
        stripped = stripped[newline + 1:]
    if stripped.rstrip().endswith("```"):
        stripped = stripped.rstrip()[:-3]
    return stripped.strip()
