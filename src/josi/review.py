"""Independent review: policy, reviewer prompt, and strict verdict parsing.

After validation passes, the reviewer runs in a temporary worktree checked
out at the validated commit (never in the task worktree). It receives the
task, the cumulative diff of the task branch against its base commit, the
validation results, and the review policy.

The reviewer must end its turn with exactly one complete JSON object.
Parsing is strict: prose around the verdict, several objects, truncated
JSON, unknown decisions or severities, approvals that carry blocking
findings, and change requests without a blocking finding are all rejected,
so none of them can ever be mistaken for an approval. The orchestration
layer may request one formatting-only correction in the same session;
the corrected message must pass the same strict parser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DECISION_APPROVED = "approved"
DECISION_CHANGES_REQUESTED = "changes_requested"
DECISIONS = (DECISION_APPROVED, DECISION_CHANGES_REQUESTED)
SEVERITY_CRITICAL = "critical"
SEVERITY_MAJOR = "major"
SEVERITY_MINOR = "minor"
SEVERITY_SUGGESTION = "suggestion"
SEVERITIES = (
    SEVERITY_CRITICAL, SEVERITY_MAJOR, SEVERITY_MINOR, SEVERITY_SUGGESTION,
)
#: Findings that block approval.
BLOCKING_SEVERITIES = (SEVERITY_CRITICAL, SEVERITY_MAJOR)
#: Optional project-level policy file, relative to the project root.
POLICY_FILE = "REVIEW_POLICY.md"

# The built-in risk-based review policy. It deliberately keeps approval
# cheap: only credible critical or major findings reject a task, so minor
# findings and suggestions can never trigger repair iterations.
BUILTIN_POLICY = """\
Review priorities, in order:
1. Does the implementation satisfy the task requirements?
2. Does it work correctly under normal operating conditions?
3. Does it handle common failures reasonably?
4. Does it introduce significant security, correctness, or data-loss risks?
5. Do meaningful tests cover the requested behavior and relevant failures?
   Assess test adequacy and flag unjustified coverage reductions or tests
   weakened or deleted merely to conceal defects.

Focus on changes introduced by the task. Do not audit unrelated code.

Severity levels:
- CRITICAL: Serious security vulnerability, significant data-loss risk, or severe correctness failure.
- MAJOR: Unmet core requirement, broken important functionality, or significant defect under realistic operating conditions.
- MINOR: Uncommon edge case, limited-impact defect, or nonessential robustness issue.
- SUGGESTION: Optional improvement, refactoring, or style preference.

Approval rules:
- Reject only for CRITICAL or MAJOR findings, failed required validations, or unmet mandatory acceptance criteria.
- Approve when only MINOR findings or SUGGESTIONS remain.
- Approval means sufficiently reliable, not perfect.

Blocking findings must include a concrete failure scenario, realistic impact, and relevant code location. Evaluate both likelihood and severity. Reproducibility alone does not justify MAJOR severity.

Review principles:
- Prioritize normal usage, expected concurrency, and common errors.
- Do not block on extremely unlikely edge cases with limited impact.
- Do not require protection against arbitrary combinations of exceptional events.
- Respect third-party dependency boundaries; avoid invasive workarounds for obscure library behavior.
- Consider proportionality: fixes should not introduce excessive complexity relative to the risk.
- Do not request unrelated changes, speculative improvements, or unnecessary defensive programming.
- Apply consistent severity standards across iterations. Do not progressively escalate into increasingly obscure scenarios.

**The goal is working, maintainable software, not theoretical perfection. Approve when the implementation is good enough for its intended use.**"""


class ReviewError(Exception):
    """Raised when reviewer output or review configuration is unusable."""


class ReviewFormatError(ReviewError):
    """The final message is not one JSON object and may be reformatted once."""


@dataclass(frozen=True)
class Finding:
    """One reviewer finding. Scenario/impact/location matter for blocking ones."""

    severity: str
    description: str
    scenario: str = ""
    impact: str = ""
    location: str = ""

    def as_dict(self) -> dict:
        """Return the finding as a JSON-safe dict for storage."""
        data = {"severity": self.severity, "description": self.description}
        if self.scenario:
            data["scenario"] = self.scenario
        if self.impact:
            data["impact"] = self.impact
        if self.location:
            data["location"] = self.location
        return data


@dataclass(frozen=True)
class Verdict:
    """A validated reviewer verdict."""

    decision: str
    findings: tuple

    @property
    def blocking(self) -> tuple:
        """The critical and major findings, in the reviewer's order."""
        return tuple(
            finding for finding in self.findings
            if finding.severity in BLOCKING_SEVERITIES
        )


def load_project_policy(root: Path):
    """Return the contents of the project's REVIEW_POLICY.md, or None.

    Raises :class:`ReviewError` when the file exists but cannot be read: a
    policy that silently disappears would weaken the review.
    """
    path = root / POLICY_FILE
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReviewError("cannot read " + str(path) + " (" + str(exc) + ")")


def policy_text(project_policy=None) -> str:
    """Return the built-in policy plus an optional project policy.

    The built-in rules always stay in force; the project policy only adds
    project-specific guidance.
    """
    if project_policy is None or not project_policy.strip():
        return BUILTIN_POLICY
    return (
        BUILTIN_POLICY
        + "\n\nProject review policy (" + POLICY_FILE + "):\n"
        + project_policy.strip()
    )


def build_prompt(task, base_commit, commit, diff, validation_summary, policy) -> str:
    """Build the reviewer's single-turn prompt."""
    description = task.description.strip() or "(none)"
    return (
        "You are an independent code reviewer for an automated coding"
        " workflow. You review one completed task in a temporary, isolated"
        " Git checkout of the validated commit. The checkout is discarded"
        " after your review and the real task worktree is never touched.\n"
        "\n"
        "Task t" + str(task.id) + ": " + task.title + "\n"
        "\n"
        "Requirements and acceptance criteria:\n"
        + description + "\n"
        "\n"
        "The task branch was created from base commit " + base_commit + " and"
        " the validated commit is " + commit + ". You review the complete"
        " cumulative change of the task implementation, not individual"
        " commits:\n"
        "\n"
        "```diff\n" + diff + "\n```\n"
        "\n"
        "Validation results:\n" + validation_summary + "\n"
        "\n"
        "Inspect files in this checkout whenever the diff is not enough"
        " context. Judge the changes this task introduces; use the rest of"
        " the repository only as context.\n"
        "Assess whether meaningful tests cover the requested behavior and"
        " relevant failures. Assess test adequacy and unjustified coverage"
        " reductions, including tests weakened or deleted to conceal defects.\n"
        "\n"
        "Review policy:\n" + policy + "\n"
        "\n"
        "When you finish, reply with exactly one JSON object and nothing"
        " else. Your final message is parsed by a machine: do not include"
        " a test summary, explanation, introductory prose, or Markdown"
        " before or after the JSON. Put explanations inside the findings."
        " Use one of these forms:\n"
        '{"decision": "approved", "findings": []}\n'
        '{"decision": "approved", "findings": [{"severity": "minor",'
        ' "description": "..."}]}\n'
        '{"decision": "changes_requested", "findings": [{"severity":'
        ' "major", "description": "...", "scenario": "...", "impact":'
        ' "...", "location": "src/file.py"}]}\n'
        "Every critical or major finding must include scenario, impact, and"
        " location. Use changes_requested only when at least one finding is"
        " critical or major; otherwise approve and list the remaining"
        " findings as minor or suggestion."
    )


def format_retry_prompt() -> str:
    """Ask the same reviewer to fix formatting, not reconsider its review."""
    return (
        "Your final message was rejected because it was not one complete"
        " JSON object. Re-emit the verdict from your review as exactly one"
        " JSON object with decision and findings, and nothing else.\n"
        "Preserve the decision and every finding, including severity,"
        " description, scenario, impact, and location. Do not change the"
        " review decision, omit or downgrade findings, or redo the review."
        " Do not use tools or modify files. Do not include a test summary,"
        " introductory prose, Markdown fences, or any text outside the JSON."
    )


def parse_verdict(text: str) -> Verdict:
    """Parse and validate one reviewer verdict.

    Raises :class:`ReviewError` for anything that is not a complete,
    consistent verdict. In particular an approved verdict that carries a
    critical or major finding and a changes_requested verdict without one are
    both refused, and the orchestration layer never records them.
    """
    payload = _verdict_object(text)
    decision = payload.get("decision")
    if decision not in DECISIONS:
        raise ReviewError("unknown review decision " + repr(decision))
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise ReviewError("the verdict 'findings' must be a list")
    findings = tuple(_parse_finding(item) for item in raw_findings)
    verdict = Verdict(decision=decision, findings=findings)
    blocking = verdict.blocking
    if decision == DECISION_APPROVED and blocking:
        raise ReviewError(
            "an approved verdict contains a " + blocking[0].severity + " finding"
        )
    if decision == DECISION_CHANGES_REQUESTED and not blocking:
        raise ReviewError(
            "a changes_requested verdict has no critical or major finding"
        )
    return verdict


def verdict_from_output(output: str, final_text=None) -> Verdict:
    """Extract the reviewer's final message from its output and parse it.

    ``output`` is the complete stdout/stderr of ``opencode run --format json``:
    JSONL events with any non-JSON stderr lines mixed in.
    """
    text = (final_text or _final_text)(output)
    if not text.strip():
        raise ReviewError("the reviewer produced no text output")
    return parse_verdict(text)


def _final_text(output: str) -> str:
    """Return the text of the reviewer's final message in JSON event output.

    Text parts of the last assistant message are joined; earlier commentary
    and tool output are ignored. Non-JSON lines (stderr) are skipped.
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


def _parse_finding(item) -> Finding:
    if not isinstance(item, dict):
        raise ReviewError("every finding must be a JSON object")
    severity = item.get("severity")
    if severity not in SEVERITIES:
        raise ReviewError("unknown finding severity " + repr(severity))
    fields = {}
    for name in ("description", "scenario", "impact", "location"):
        value = item.get(name, "")
        if not isinstance(value, str):
            raise ReviewError("finding '" + name + "' must be a string")
        fields[name] = value.strip()
    if not fields["description"]:
        raise ReviewError("a finding is missing its description")
    if severity in BLOCKING_SEVERITIES:
        for name in ("scenario", "impact", "location"):
            if not fields[name]:
                raise ReviewError(
                    "a " + severity + " finding must include '" + name + "'"
                )
    return Finding(severity=severity, **fields)


def _strip_code_fence(text: str) -> str:
    """Remove one surrounding Markdown code fence, when present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    newline = stripped.find("\n")
    if newline >= 0:
        stripped = stripped[newline + 1:]
    if stripped.rstrip().endswith("```"):
        stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def _verdict_object(text: str):
    """Return the single JSON object the reviewer finished with.

    The final message must be one complete JSON object; a single surrounding
    Markdown code fence is tolerated. Prose, several objects, truncated JSON
    and non-objects are all refused, so a partial or ambiguous answer can
    never pass as a verdict.
    """
    stripped = _strip_code_fence(text.strip())
    try:
        payload = json.loads(stripped)
    except ValueError:
        raise ReviewFormatError(
            "the reviewer output is not one complete JSON verdict"
        )
    if not isinstance(payload, dict):
        raise ReviewFormatError("the reviewer verdict is not a JSON object")
    return payload
