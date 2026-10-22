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
- `actl plan` opens the interactive OpenCode TUI from the project root and
  waits for your input - it never sends an initial message. The planner
  instructions live in a dedicated read-only OpenCode custom agent (see
  below). Model selection stays interactive inside OpenCode: actl never
  passes `--model`.
- Each project keeps **one active planner session**. The session ID (generated
  by OpenCode) is stored in `.actl/planner.json`. Subsequent `actl plan` runs
  resume that session; `actl plan --new` creates a new one and makes it active,
  leaving earlier OpenCode sessions untouched.

## Planner agent

On the first `actl plan` (or whenever the file is missing), actl installs

```
.opencode/agents/actl-planner.md
```

an OpenCode custom primary agent. Its body is the planner's **system prompt**
(define requirements, decompose goals into tasks with acceptance criteria,
never modify code, never execute tasks), and its frontmatter makes the agent
read-only: `edit` is denied and every shell command requires explicit user
approval - except safe Git inspection commands (`git status`, `git log`,
`git diff`, `git show`, `git blame`, `git rev-parse`, `git ls-files`,
`git grep`), which are pre-approved. The prompt also tells the planner to
prefer the native `read`, `glob`, and `grep` tools over shell `cat`, `ls`, or
`grep`, so routine exploration does not trigger prompts. Because the
instructions are system-level, launching the planner does not start a
conversation by itself - the TUI just waits for input.

actl binds the planner session to this agent (for `--new` and first runs) and
re-selects it before resuming, so older sessions created before the agent
existed are upgraded on the next `actl plan`. An existing
`.opencode/agents/actl-planner.md` is never overwritten, so you can customize
your planner; delete the file to have actl restore the default.

## How it works

actl delegates all session management to OpenCode - it does not store
conversations itself. It uses these OpenCode surfaces:

| Purpose | Command |
| --- | --- |
| Create a session at the project root | `opencode api POST /api/session` (explicit `location.directory` and `agent`) |
| Verify a stored session belongs to the project | `opencode api GET /api/session/<id>` |
| Re-select the planner agent on an old session | `opencode api POST /api/session/<id>/agent` |
| Open the planner TUI | `opencode <project-root> --session <id>` |

Sessions are created explicitly (instead of letting the TUI create them
lazily) so actl captures the real session ID. Before resuming, actl asks
OpenCode for the session and compares its `location.directory` with the
project root; if the session is missing, unreadable, or belongs elsewhere,
actl reports an error instead of resuming the wrong session.

## Project files

```
.actl/
├── project.json   # schema_version, project name, created_at
└── planner.json   # active planner session id, created_at

.opencode/
└── agents/
    └── actl-planner.md   # planner agent: system prompt + read-only permissions
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
