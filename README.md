# actl

A lightweight Python CLI for orchestrating AI coding agents through
[OpenCode](https://opencode.ai). Stage 1 covers project setup and planning.

## Requirements

- Python 3.9+
- Git
- The OpenCode CLI (v2) on `PATH` (tested with OpenCode 2.1.2)

## Install

```sh
pip install -e .
# or run without installing:
PYTHONPATH=src python -m actl --help
```

## Usage

```sh
cd your-project
actl init          # register the Git repository as an actl project
actl plan          # start (or resume) the planner session in OpenCode
actl plan --new    # start a fresh planner session
```

- `actl init` requires a Git repository, creates `.actl/project.json`, and adds
  `.actl/` to `.gitignore` (existing `.gitignore` content is preserved).
  Running it again is harmless. It also checks that OpenCode is installed.
- `actl plan` launches the interactive OpenCode TUI from the project root with
  a fixed planner instruction prompt. Model selection stays interactive inside
  OpenCode: actl never passes `--model`.
- Each project keeps **one active planner session**. The session ID (generated
  by OpenCode) is stored in `.actl/planner.json`. Subsequent `actl plan` runs
  resume that session; `actl plan --new` creates a new one and makes it active,
  leaving earlier OpenCode sessions untouched.

## How it works

actl delegates all session management to OpenCode - it does not store
conversations itself. It uses three OpenCode surfaces:

| Purpose | Command |
| --- | --- |
| Create a session at the project root | `opencode api POST /api/session` (with an explicit `location.directory`) |
| Verify a stored session belongs to the project | `opencode api GET /api/session/<id>` |
| Open the planner TUI | `opencode <project-root> --session <id> [--prompt <text>]` |

Sessions are created explicitly (instead of letting the TUI create them
lazily) so actl captures the real session ID. Before resuming, actl asks
OpenCode for the session and compares its `location.directory` with the
project root; if the session is missing, unreadable, or belongs elsewhere,
actl reports an error instead of resuming the wrong session.

The planner instruction prompt is sent when a session is first created
(including via `--new`). Resumed sessions are not re-prompted, so the
instruction does not pile up in the conversation.

## Project files

```
.actl/
├── project.json   # schema_version, project name, created_at
└── planner.json   # active planner session id, created_at
```

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests use a fake `opencode` executable on `PATH`, so they need no network
access, no real OpenCode process, and no model calls.

## Limitations

- Targets OpenCode v2; the `opencode api` surface is still marked experimental
  by OpenCode. If a future OpenCode release changes it, session creation and
  verification will fail with a clear error rather than do the wrong thing.
- One planner session per project. Replacing it with `--new` simply forgets
  the old session ID; the old session remains available inside OpenCode.
- actl must be run from inside the project; it finds `.actl/` by walking up
  from the current directory. The project root is the Git repository root.
- No task management, worker execution, or validation yet - that is Stage 2.
