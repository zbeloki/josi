# actl

A lightweight Python CLI for orchestrating AI coding agents through
[OpenCode](https://opencode.ai). Stage 1 covers project setup and planning;
Stage 2 adds a project-local task list.

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
actl task list     # list the project's tasks
```

- `actl init` requires a Git repository, creates `.actl/project.json`, and adds
  `.actl/` to `.gitignore` (existing `.gitignore` content is preserved).
  Running it again is harmless. It also checks that OpenCode is installed.
- `actl plan` opens the interactive OpenCode TUI from the project root and
  waits for your input - it never sends an initial message. The planner
  instructions live in a dedicated read-only OpenCode custom agent: editing,
  subagents, web access, and MCP tools are denied, and shell commands ask for
  approval. Model selection stays interactive inside OpenCode: actl never
  passes `--model`.
- Each project keeps **one active planner session**. The session ID (generated
  by OpenCode) is stored in `.actl/planner.json`. Subsequent `actl plan` runs
  resume that session; `actl plan --new` creates a new one and makes it active,
  leaving earlier OpenCode sessions untouched.

## Tasks

Stage 2 stores the project's tasks in a SQLite database (`.actl/tasks.db`).
Task commands are noninteractive: everything comes from arguments and files,
output is plain text, and problems are reported on stderr with a non-zero
exit code.

```sh
actl task create --title "Add the login form" --depends-on t1
actl task create --title "Write the login tests" --description-file tests.md
actl task list
actl task show t1
actl task update t2 --title "New title" --depends-on t1 --depends-on t3
actl task delete t2
```

- Tasks have an integer id, printed as `t1`, `t2`, ...; commands accept both
  `t1` and `1`. New tasks are `PENDING` - Stage 2 tracks no other status.
- `--description-file PATH` reads the description from a file (trailing
  newlines are stripped) and cannot be combined with `--description`.
- `--depends-on ID` may be repeated or comma-separated. On `create` it adds
  dependencies; on `update` it replaces them. `--clear-dependencies` removes
  them all.
- A dependency must reference an existing task and must not create a cycle
  (directly or indirectly); self-dependencies are rejected. Deleting a task
  that other tasks depend on is refused until those dependents are deleted.
- A task is blocked when a dependency is unfinished; because Stage 2 never
  completes tasks, blockedness is derived from dependencies instead of being
  stored as a separate status.
- The database is created automatically when needed, including for projects
  initialized by Stage 1. Repeated `actl init` runs and task commands never
  reset existing tasks.
- Task commands never invoke OpenCode and never touch another project's
  database: they resolve the Git repository root first and require `.actl/`
  there, so a nested repository is never served from its parent's database.

## Planner agent

On the first `actl plan` (or whenever the file is missing), actl installs

```
.opencode/agents/actl-planner.md
```

an OpenCode custom primary agent. Its body is the planner's **system prompt**
(define requirements, decompose goals into tasks with acceptance criteria,
never modify code, never execute tasks). Its permissions are deny-by-default:
a catch-all rule blocks every tool, and only the native read-only inspection
tools (`read`, `glob`, `grep`) and questions are re-allowed. Editing,
subagents, web access, and MCP tools stay denied - including tools that write
files under an action name other than `edit`. Every shell command requires
explicit user approval, and reading a `.env` file still asks. The prompt also
tells the planner to prefer the native `read`, `glob`, and `grep` tools over
shell `cat`, `head`, `tail`, `ls`, or `grep` (the `read` tool pages through
files with offset/limit), and to run inspections one per shell call (each
command in a compound `&&` call is checked separately, and helpers like
`echo` always require confirmation). Because the instructions are
system-level, launching the planner does not start a conversation by itself -
the TUI just waits for input.

actl deliberately pre-approves no shell commands: OpenCode matches a broad
allow rule like `git diff *` or `cat *` against commands that write files as
well (`git diff --output=file`, `cat file > file`), which would silently
defeat the no-modification rule. Approve individual inspections in the
permission dialog; choose *Allow always* there if you want a specific command
pre-approved - OpenCode saves a project-scoped rule that actl does not
override.

actl binds the planner session to this agent (for `--new` and first runs) and
re-selects it before resuming, so older sessions created before the agent
existed are upgraded on the next `actl plan`.

The agent file is **managed by actl**: if its content differs from the
definition shipped with the installed actl version (for example after an
upgrade changes the permissions or the prompt), the next `actl plan` rewrites
it and prints an `Updated the planner agent definition` notice. Do not hand-edit
that file; put project-specific guidance in your own `AGENTS.md` instead.

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
├── planner.json   # active planner session id, created_at
└── tasks.db       # SQLite task database

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
- actl must be run from inside the project. It locates the Git repository
  root (even from a subdirectory) and requires `.actl/` there, so a nested
  Git repository is never treated as part of its parent's actl project.
- actl writes its managed files (`.actl/`, `.gitignore`, and the planner
  agent) with plain file writes that follow symlinks. A repository containing
  a hostile symlink at one of those paths can redirect the write. Run actl
  only in trusted repositories. Accepted for Stage 1.
- No task execution, validation, scheduling, or planner integration yet -
  Stage 2 only manages the task list.
