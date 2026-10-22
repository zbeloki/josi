# actl

A lightweight Python CLI for orchestrating AI coding agents through
[OpenCode](https://opencode.ai). Stage 1 covers project setup and planning;
Stage 2 adds a project-local task list; Stage 3 lets the planner manage that
task list through the same `actl task` CLI; Stage 4 runs one task at a time
with a noninteractive OpenCode worker in an isolated Git worktree.

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
actl plan          # start (or resume) the planner; it can manage tasks
actl plan --new    # start a fresh planner session
actl task list     # list the project's tasks
actl task run t1   # execute a task with the worker
```

- `actl init` requires a Git repository, creates `.actl/project.json`, and adds
  `.actl/` to `.gitignore` (existing `.gitignore` content is preserved).
  Running it again is harmless. It also checks that OpenCode is installed.
  `actl init --worker-model MODEL` sets (or updates) the project's worker
  model, for example `actl init --worker-model openrouter/anthropic/claude-sonnet-latest`.
  `actl init --base BRANCH` sets the base branch that task branches are
  created from; without it the branch is detected, and when the choice is
  ambiguous actl asks. It works in repositories without commits, where it
  records the unborn `HEAD` branch and never creates a commit itself.
- `actl plan` opens the interactive OpenCode TUI from the project root and
  waits for your input - it never sends an initial message. The planner
  instructions live in a dedicated OpenCode custom agent: editing, subagents,
  web access, and MCP tools are denied, and shell commands ask for approval
  except the `actl task` management commands, which are pre-approved
  (`actl task run` stays denied). Model selection stays interactive inside
  OpenCode: actl never passes `--model`.
- Each project keeps **one active planner session**. The session ID (generated
  by OpenCode) is stored in `.actl/planner.json`. Subsequent `actl plan` runs
  resume that session; `actl plan --new` creates a new one and makes it active,
  leaving earlier OpenCode sessions untouched.

## Tasks

Stage 2 stores the project's tasks in a SQLite database (`.actl/tasks.db`).
Task commands are noninteractive: everything comes from arguments and files,
output is plain text, and problems are reported on stderr with a non-zero
exit code.

The planner manages the same task list with the same commands: the
`actl task` management commands are the only shell commands it may run
without a permission prompt, so task planning and task management happen in
one OpenCode session. (`actl task run` is denied to the planner.)

```sh
actl task create --title "Add the login form" --depends-on t1
actl task create --title "Write the login tests" --description-file tests.md
actl task list
actl task show t1
actl task update t2 --title "New title" --depends-on t1 --depends-on t3
actl task delete t2
```

- Tasks have an integer id, printed as `t1`, `t2`, ...; commands accept both
  `t1` and `1`. New tasks are `PENDING`; `actl task run` is the only command
  that changes a task's status.
- `--description-file PATH` reads the description from a file (trailing
  newlines are stripped) and cannot be combined with `--description`.
- `--depends-on ID` may be repeated or comma-separated. On `create` it adds
  dependencies; on `update` it replaces them. `--clear-dependencies` removes
  them all.
- A dependency must reference an existing task and must not create a cycle
  (directly or indirectly); self-dependencies are rejected. Deleting a task
  that other tasks depend on is refused until those dependents are deleted.
- A task is blocked when a dependency is unfinished; blockedness is derived
  from dependencies instead of being stored as a separate status, and
  `actl task run` refuses blocked tasks.
- The database is created automatically when needed, including for projects
  initialized by Stage 1. Repeated `actl init` runs and task commands never
  reset existing tasks.
- Task commands never touch another project's database: they resolve the Git
  repository root first and require `.actl/` there, so a nested repository is
  never served from its parent's database. Only `actl task run` invokes
  OpenCode.

## Running tasks

`actl task run <id>` executes one task with a noninteractive OpenCode worker
in an isolated Git worktree. No validation, review, or merging happens yet.

```sh
actl task run t1   # first execution, or an explicit retry after a failure
```

A run goes through these steps:

1. The project must be initialized and the task must exist, be runnable, and
   have no dependencies.
2. The project-wide execution lock is taken. A second `actl task run` in the
   same project refuses immediately while an execution is active.
3. The task gets a dedicated branch (`actl/t1`) and worktree
   (`.actl/worktrees/t1`) created from the latest local base branch (the
   project's configured `base_branch`). They are created the first time the
   task runs - never at `actl task create`.
4. OpenCode runs one noninteractive turn inside the worktree with the
   project's worker model and the built-in `build` agent
   (`opencode run --agent build --model MODEL --session ID`).
5. The worker's complete stdout and stderr are written straight into a log
   file under `.actl/runs/` while it runs; the log path is printed when the
   worker starts, so the run can be followed with `tail -f <logfile>`.
6. The task status is updated.

The terminal shows only actl's own timestamped events - task and model,
worktree preparation, worker start, worker exit code, and the final task
status - plus the run log path. The worker's transcript is never printed to
the terminal.

The worker prompt contains the task title, its description, and the worker
instructions: implement the task, run the relevant checks, commit the changes
on the branch, keep the worktree clean, and never merge into the base branch.

The lifecycle is:

```text
PENDING → RUNNING → IMPLEMENTED
                  ↘ FAILED
```

- `IMPLEMENTED` means the worker exited successfully, left a clean task
  worktree, and left committed changes on the task branch relative to the
  base branch. It does not imply validation or approval.
- A task that fails can be retried explicitly with `actl task run <id>`: the
  existing branch and worktree are reused as they are, so earlier commits are
  preserved and the worker continues from there. Nothing is reset, cleaned,
  rebased, or merged.
- Tasks with dependencies stay blocked. Dependency-aware execution arrives in
  a later stage (Stage 7, when successfully merged tasks become `DONE`); in
  Stage 4 only tasks without dependencies can run.
- An `IMPLEMENTED` task cannot be run again. The task keeps its branch and
  worktree after success or failure; actl never removes either.
- If an execution is interrupted (for example by Ctrl-C or a crash), the task
  keeps `RUNNING` and its worktree and log are preserved; there is no
  automatic recovery, but an explicit `actl task run <id>` continues from the
  preserved worktree.
- The task is marked `IMPLEMENTED` only when OpenCode exits with status 0,
  the worktree is still on its task branch, the worktree is clean, and the
  branch has committed changes relative to the base branch. A worker that
  leaves uncommitted or untracked files behind, or commits nothing, is a
  failure. A retry may finish with previously committed work and no new
  commit.

The worker model is project-level configuration:

```sh
actl init --worker-model openrouter/anthropic/claude-sonnet-latest
actl init --worker-model provider/model#variant   # with a model variant
```

It is stored as `worker_model` in `.actl/project.json` (the file may be
edited directly as well). `actl task run` refuses to start until a model is
configured, and OpenCode fails the run if the model is not available in the
worktree.

The base branch is project-level configuration too:

```sh
actl init --base develop
```

It is stored as `base_branch` in `.actl/project.json` (the file may be edited
directly as well). Without `--base`, `actl init` uses the remote default
branch (`origin/HEAD`) when the matching local branch exists, otherwise
`main` or `master` when exactly one of them exists; when the choice is
ambiguous it lists the existing branches and asks. It never assumes the
currently checked-out branch. In a repository without commits it records the
unborn branch `HEAD` points at. Task branches are always created from the
configured base branch, and `actl task run` refuses to run while that branch
has no commits - create an initial commit first, since `actl init` never
commits. Re-running `actl init` without `--base` never changes an existing
selection; a project initialized before `base_branch` existed gains one on
the next `actl init`, and `actl task run` keeps working by detecting the
branch without writing metadata.

Every execution writes `.actl/runs/t1-<timestamp>.log`: a short header
(task, model, branch, worktree, session), the complete worker stdout and
stderr, and the result (exit code, final status, failure reason). The log is
written continuously while the worker runs, and its path is printed when the
worker starts as well as in the final summary; use `tail -f` to watch a run.

Only one execution may run per project. The lock is an exclusive, nonblocking
`fcntl.flock` on `.actl/execution.lock`, and the locked file descriptor is
passed to the OpenCode process (Python's `pass_fds`), so the lock stays held
while that process is alive even if actl itself is killed.

## Planner agent

On the first `actl plan` (or whenever the file is missing), actl installs

```
.opencode/agents/actl-planner.md
```

an OpenCode custom primary agent. Its body is the planner's **system prompt**:
discuss requirements and design solutions, decompose goals into tasks with
descriptions and acceptance criteria, manage the task list only through
`actl task`, never modify code, and never execute tasks. Its permissions are
deny-by-default: a catch-all rule blocks every tool, and only the native
read-only inspection tools (`read`, `glob`, `grep`), questions, and the
`actl task` management subcommands are re-allowed. Editing, subagents, web
access, and MCP tools stay denied - including tools that write files under an
action name other than `edit` - and reading a `.env` file still asks.
`actl task run` is denied outright, so the planner can manage the task list
but can never launch a worker.

The `actl task create`, `actl task list`, `actl task show`,
`actl task update`, and `actl task delete` subcommands are the only
pre-approved shell commands: the planner can use them without a permission
prompt, while every other shell command still requires explicit user approval.
A broad `actl task *` deny comes before those management allows (OpenCode
evaluates permission rules last-match-wins), so only the documented forms are
pre-approved and `actl task run` - including whitespace variants such as
`actl task  run` - stays denied. The deny was verified against the installed
OpenCode 2.1.2 permission engine.
The prompt tells the planner to prefer the native `read`, `glob`, and `grep`
tools over shell `cat`, `head`, `tail`, `ls`, or `grep` (the `read` tool
pages through files with offset/limit), and to run inspections one per shell
call (each command in a compound `&&` call is checked separately, and helpers
like `echo` still require confirmation). Because the instructions are
system-level, launching the planner does not start a conversation by itself -
the TUI just waits for input.

Shell **redirection** (`>`, `>>`, `2>&1`) is denied outright. OpenCode
reports a redirected statement as a single resource string, so a management
allow rule like `actl task list *` would also match
`actl task list > src/file` and could silently overwrite source files; the
explicit deny rule comes after the management allows (OpenCode evaluates
permission rules last-match-wins), which closes that hole. As a consequence,
a literal `>` inside a quoted task title or description counts as redirection
and is denied too; reword the text or run that command yourself outside the
planner.

actl deliberately pre-approves no other shell commands: OpenCode matches a
broad allow rule like `git diff *` or `cat *` against commands that write
files as well (`git diff --output=file`, `cat file > file`), which would
silently defeat the no-modification rule. Approve individual inspections in
the permission dialog; choose *Allow always* there if you want a specific
command pre-approved - OpenCode saves a project-scoped rule that actl does
not override (it is evaluated after the agent rules but cannot override an
agent deny).

The rule format is verified against the installed OpenCode 2.1.2: a
permission rule is `{action, resource, effect}` with effect `allow`, `deny`,
or `ask`; action and resource are matched as glob patterns (`*`, `?`) and
the last matching rule wins, so ordering matters. Shell commands are checked
one resource per command, with `action: shell` and the parsed command text
as the resource.

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
| Create a worker session in a task worktree | `opencode api POST /api/session` (location and agent `build`) |
| Run the worker noninteractively | `opencode run --agent build --model <model> --session <id> "<prompt>"` |

Sessions are created explicitly (instead of letting the TUI create them
lazily) so actl captures the real session ID. Before resuming, actl asks
OpenCode for the session and compares its `location.directory` with the
project root; if the session is missing, unreadable, or belongs elsewhere,
actl reports an error instead of resuming the wrong session.

## Project files

```
.actl/
├── project.json    # schema_version, name, worker_model, base_branch, created_at
├── planner.json    # active planner session id, created_at
├── tasks.db        # SQLite task database
├── execution.lock  # project-wide execution lock (fcntl.flock)
├── runs/           # one log per execution: tN-<timestamp>.log
└── worktrees/      # one Git worktree per task: t1/, t2/, ...

.opencode/
└── agents/
    └── actl-planner.md   # planner agent: system prompt + task permissions
```

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests use a fake `opencode` executable on `PATH`, so they need no network
access, no real OpenCode process, and no model calls. The planner tests
evaluate the installed agent's permission rules with a matcher that mirrors
OpenCode 2.1.2 (last matching rule wins, `*`/`?` globs, `ask` by default) and
run the pre-approved `actl task` commands against a real project database.
The Stage 4 tests drive `actl task run` against disposable Git repositories;
the fake worker can commit, leave a dirty worktree, fail, or keep running
while the test kills actl, so branch and worktree handling, retries, and the
execution lock (including the inherited lock descriptor) are all exercised
without a real model. Base-branch selection is covered too: `main`/`master`
detection, feature branches, the ambiguity prompt, explicit `--base`, the
remote default branch, repositories without commits, and legacy projects
without a stored `base_branch`.

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
- The planner is a language model guided by the agent instructions; those
  instructions are not a sandbox. The permission layer blocks file editing,
  task execution tools, and shell redirection, but review what the planner
  proposes before approving anything it asks for.
- Stage 4 executes one task at a time and stops there. There is no validation,
  no independent review, no automatic retry or scheduling, and no automatic
  merging; a task with dependencies cannot run until a later stage introduces
  `DONE` after merging.
- `IMPLEMENTED` only means that the worker exited successfully, left the
  worktree clean, and left committed changes on the task branch relative to
  the base branch. It says nothing about whether the implementation is
  correct.
- actl never resets, cleans, rebases, or deletes a task worktree, and it never
  touches the base branch. A task that fails keeps its branch, worktree, and
  log for an explicit retry.
- The execution lock covers actl and the OpenCode process it spawns. A
  detached descendant that does not keep the inherited descriptor (for example
  a daemonized helper the worker starts) can outlive the lock; actl keeps no
  PID tracking, heartbeats, or automatic recovery.
- The worker runs with OpenCode's standard `build` agent, so it may modify
  anything inside its worktree. A noninteractive run cannot ask for
  permissions: a request that would prompt in the TUI (for example reading a
  `.env` file or touching a path outside the worktree) is auto-rejected and
  the worker continues without it. actl does not pass `--auto`, so those
  guards stay active. Like the planner, this is not a sandbox - review the
  commits on the task branch before merging anything.
- The worker prompt asks the worker to commit everything and never merge into
  the base branch, and actl checks afterwards that the worktree is still on
  its task branch, is clean, and contains committed changes relative to the
  base branch; a worker that ignores the instructions is reported as a
  failure rather than corrected automatically.
