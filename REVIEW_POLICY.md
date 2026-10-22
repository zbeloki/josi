# actl Review Policy

## Project context

actl is a lightweight CLI for personal use on trusted Linux
development environments.

It is not a security sandbox, multi-user service, or
production orchestration platform.

Simplicity is a primary design requirement.

## Review priorities

Focus on:

1. Core functionality failing under normal usage.
2. Accidental data loss or corruption.
3. Modifications to the wrong repository or branch.
4. Dangerous Git operations.
5. Incorrect task execution, validation, or review.
6. Security vulnerabilities with credible exploitation paths.

## Severity

CRITICAL:
Credible risk of data loss, credential exposure,
or destructive behavior.

MAJOR:
Core functionality fails under realistic supported conditions.

MINOR:
Rare edge cases, recoverable failures, or robustness improvements.

OUT OF SCOPE:
Explicitly unsupported environments or accepted limitations.

## Review rules

- Evaluate likelihood AND impact.
- Require a concrete reproduction for blocking findings.
- Prefer simple fixes over defensive frameworks.
- Do not recommend speculative hardening.
- Do not demand features from future stages.
- Do not treat every possible exception as a release blocker.
- Do not repeat previously accepted limitations unless new
  evidence materially changes their risk.
- Do not hide credible security or data-loss risks.

## Acceptance criteria

A stage is accepted when:
- No critical or major issues remain.
- Core tests pass.
- Remaining minor issues are acknowledged.