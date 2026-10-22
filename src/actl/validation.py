"""Project validation commands.

After a worker succeeds (or when an IMPLEMENTED task is run again), the
project's configured validation commands run sequentially inside the task
worktree. The first non-zero exit stops the run and fails the task; every
command's output and exit code is written to a validation log.

Configuration lives in the project's existing metadata (``.actl/project.json``)::

    {
      "validation": {
        "commands": ["pytest", "ruff check ."]
      }
    }

A missing configuration (or an empty list) means validation is skipped and
the run proceeds to review. A malformed configuration is an error: silently
skipping validation because of a typo would let a broken configuration pass
as a clean run.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


class ValidationError(Exception):
    """Raised when the validation configuration is invalid."""


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of one validation run.

    ``results`` holds one ``(command, exit_code)`` pair per executed command,
    in execution order. ``failed`` names the first command that exited
    non-zero, or is empty when every command passed.
    """

    results: tuple
    failed: str

    @property
    def passed(self) -> bool:
        return not self.failed


def commands_from_metadata(metadata: dict) -> list:
    """Return the project's configured validation commands.

    Returns an empty list when validation is not configured at all.
    """
    config = metadata.get("validation")
    if config is None:
        return []
    if not isinstance(config, dict):
        raise ValidationError(
            "project metadata 'validation' must be an object with a"
            " 'commands' list"
        )
    commands = config.get("commands", [])
    if not isinstance(commands, list):
        raise ValidationError(
            "project metadata 'validation.commands' must be a list of shell"
            " commands"
        )
    result = []
    for command in commands:
        if not isinstance(command, str) or not command.strip():
            raise ValidationError(
                "project metadata 'validation.commands' entries must be"
                " non-empty strings (found " + repr(command) + ")"
            )
        result.append(command)
    return result


def run(commands, cwd, log) -> ValidationResult:
    """Run ``commands`` sequentially in ``cwd``, writing output to ``log``.

    Stops at the first non-zero exit. Commands are shell command lines, so
    pipelines and arguments work as written; they inherit actl's environment
    and cannot read from stdin.
    """
    results = []
    for command in commands:
        log.write("$ " + command + "\n")
        log.flush()
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.write("exit_code: " + str(proc.returncode) + "\n\n")
        log.flush()
        results.append((command, proc.returncode))
        if proc.returncode != 0:
            return ValidationResult(tuple(results), command)
    return ValidationResult(tuple(results), "")


def summary(result) -> str:
    """Return a short validation summary for the reviewer prompt.

    ``None`` means validation was skipped (no commands configured).
    """
    if result is None:
        return (
            "Automated validation was skipped: no validation commands are"
            " configured for this project."
        )
    lines = [
        "- " + command + ": exit code " + str(exit_code)
        for command, exit_code in result.results
    ]
    if result.passed:
        lines.append("All configured validation commands passed.")
    else:
        lines.append("Validation failed at: " + result.failed)
    return "\n".join(lines)
