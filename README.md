# actl

A lightweight Python CLI for orchestrating AI coding agents through
[OpenCode](https://opencode.ai). Stage 1 covers project setup and planning;
Stage 2 adds a project-local task list; Stage 3 lets the planner manage that
task list through the same `actl task` CLI; Stage 4 runs one task at a time
with a noninteractive OpenCode worker in an isolated Git worktree; Stage 5
validates the worker's commit with project-configured commands and has an
independent reviewer approve or reject it; Stage 6 repairs rejected
implementations automatically with up to three worker attempts and escalates
to a stronger model on the last one; Stage 7 fast-forwards the approved
commit into the configured base branch and executes several tasks
sequentially in dependency order.

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
actl init                   # register the Git repository as an actl project
actl plan                   # start (or resume) the planner; it can manage tasks
actl plan --new             # start a fresh planner session
actl task list              # list the project's tasks
actl task run t1            # run one task: worker, validation, review, merge
actl task run t1 t2 t3      # run tasks sequentially in the given order
actl task run --all         # run every eligible task in dependency order
```

- `actl init` requires a Git repository, creates `.actl/project.json`, and adds
  `.actl/` to `.gitignore` (existing `.gitignore` content is preserved).
  Running it again is harmless. It also checks that OpenCode is installed.
  `actl init --worker-model MODEL` sets (or updates) the project's worker
  model, for example `actl init --worker-model openrouter/anthropic/claude-sonnet-latest`.
  `actl init --reviewer-model MODEL` sets the independent reviewer's model;
  `actl task run` requires it and refuses to start without one.
  `actl init --escalation-worker-model MODEL` sets the stronger model used
  for the third and final repair attempt; `actl task run` requires it and
  refuses to start without one.
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
  `actl task run` refuses blocked tasks. A dependency is satisfied only by
  `DONE`, which means the dependency's approved commit was integrated into
  the base branch; `APPROVED` alone does not unlock dependents.
- The database is created automatically when needed, including for projects
  initialized by Stage 1. Repeated `actl init` runs and task commands never
  reset existing tasks.
- Task commands never touch another project's database: they resolve the Git
  repository root first and require `.actl/` there, so a nested repository is
  never served from its parent's database. Only `actl task run` invokes
  OpenCode.

## Running tasks

`actl task run` executes tasks end to end: a noninteractive OpenCode worker
implements each one in an isolated Git worktree, the project's validation
commands run on the worker's commit, an independent reviewer approves or
rejects that exact commit, and the approved commit is fast-forwarded into
the configured base branch, which marks the task `DONE`. A failed validation
command or a reviewer rejection with critical or major findings triggers an
automatic repair: actl runs up to three worker attempts in all and escalates
the last one to a stronger model.

```sh
actl task run t1          # one task: worker, validation, review, and merge
actl task run t1 t2 t3    # the listed tasks, sequentially, in that order
actl task run --all       # every eligible task, in dependency order
```

One run of one task goes through these steps:

1. The project must be initialized with a reviewer model and an escalation
   worker model, and the task must exist, be runnable, and have only `DONE`
   dependencies. A run that starts the worker also needs the worker model;
   without a reviewer or escalation model the run refuses before any lock,
   worktree, or task change.
2. The project-wide execution lock is taken. A second `actl task run` in the
   same project refuses immediately while an execution is active, whether it
   is another single task or a multi-task batch.
3. The task gets a dedicated branch (`actl/t1`) and worktree
   (`.actl/worktrees/t1`) created from the latest local base branch (the
   project's configured `base_branch`). They are created the first time the
   task runs - never at `actl task create`.
4. OpenCode runs one noninteractive turn inside the worktree with the
   project's worker model and the built-in `build` agent
   (`opencode run --agent build --model MODEL --session ID`). A repair
   attempt uses the escalation model on the third attempt and carries the
   previous failure as feedback; the worker continues on the same branch and
   actl never resets the branch or discards earlier commits.
5. The worker's complete stdout and stderr are written straight into a log
   file under `.actl/runs/` while it runs; the log path is printed when the
   worker starts, so the run can be followed with `tail -f <logfile>`.
6. After the worker succeeds, the configured validation commands run in the
   task worktree (see below) and an independent reviewer examines the
   validated commit in a temporary worktree.
7. An approval marks the task `APPROVED` and actl integrates it: the base
   branch is fast-forwarded to the exact validated and approved commit (see
   below). A failed validation command or a rejection with critical or major
   findings starts the next worker attempt in the same branch and worktree,
   with the failure as feedback; when all three attempts are used the task
   ends `NEEDS_HUMAN`.
8. Only after the fast-forward merge succeeded is the task marked `DONE`.
   DONE means the task's approved commit is part of the configured base
   branch; it is never recorded before the integration, and a task that is
   already DONE is not executed again.

`actl task run t1` after an earlier approval does not repeat any of the
expensive work either: when the task is already `APPROVED`, actl skips the
worker, validation, and review and attempts the integration directly with
the recorded approved commit.

The terminal shows only actl's own timestamped events - task, models,
worktree preparation, worker start, worker exit code, validation start and
result, reviewer start and verdict, the fast-forward merge, and the final
task status - plus the log paths. Transcripts and validation output never
reach the terminal. A multi-task run prints a final summary of the completed
tasks and of the task that stopped it, if any.

### Automatic integration

Reaching `APPROVED` immediately integrates the task. actl merges only the
exact commit the reviewer approved:

- The approved commit comes from the recorded review. The task branch must
  still point at it; if the branch moved, nothing is merged.
- The configured base branch must exist, and the worktree that has it
  checked out must be clean (no uncommitted or untracked changes). When no
  worktree has the base branch checked out, actl uses a temporary worktree
  under `.actl/integrate/` for the merge and removes it afterwards.
- The merge is `git merge --ff-only --no-squash --no-overwrite-ignore`:
  actl never force-pushes, resets, rebases, squashes, or overwrites the
  base branch, and it never resolves conflicts. When a fast-forward is
  impossible - the base branch advanced with its own commits, or the branch
  moved after approval - actl stops and reports the problem instead of
  changing anything; the merge is likewise refused when it would replace an
  ignored local file in the base worktree. After the merge, the base branch
  must really contain the approved commit before the task is reported as
  integrated.
- An integration failure keeps the task `APPROVED`, so its branch, worktree,
  and approval are preserved. Resolving the problem (checking out the base
  branch, committing or removing local changes) and rerunning
  `actl task run <id>` merges the same approved commit without rerunning
  the worker, validation, or review.
- If Git integration succeeds but recording `DONE` fails (for example
  because actl is interrupted in between), the task stays `APPROVED`; the
  retry recognizes the completed merge and only records `DONE`. The worker
  is never rerun and no new implementation is created.

### Multiple tasks

`actl task run t1 t2 t3` executes the listed tasks sequentially in exactly
that order; actl never reorders the list. Dependencies are checked for each
task when its turn comes, so a task may depend on an earlier task of the
same list - by the time it is reached, that task has become `DONE`.
Execution stops immediately at the first task that cannot complete: the
tasks completed before it stay `DONE`, and the remaining listed tasks are
left untouched.

`actl task run --all` selects every eligible task and executes them one at a
time in dependency order:

- A task is eligible when its status is one the single-task flow accepts:
  `PENDING`, `RUNNING`, `IMPLEMENTED`, `FAILED`, `NEEDS_CHANGES`, or
  `APPROVED`. `DONE` tasks are already integrated, and `NEEDS_HUMAN` tasks
  require a human, so neither is selected automatically.
- Among the tasks whose dependencies are all `DONE`, the one with the
  smallest id runs first; the task list is re-read after every task, so a
  finished task immediately unlocks its dependents. Independent tasks
  therefore run in id order.
- A task whose dependencies stay unfinished never executes. When no selected
  task is ready, actl reports the blocked tasks and their unfinished
  dependencies and stops instead of reporting success.

Any task that fails to complete - a worker or reviewer infrastructure
failure, an exhausted repair budget, human intervention being required, a
refused or failed merge, or an unexpected state - stops the batch
immediately. The remaining tasks are left as they are, the failing task is
reported, and the command exits non-zero; actl never retries a task that
already needs a human.

The worker prompt contains the task title, its description, and the worker
instructions: implement the task, run the relevant checks, commit the changes
on the branch, keep the worktree clean, and never merge into the base branch.

The lifecycle is:

```text
PENDING → RUNNING → IMPLEMENTED → APPROVED → DONE
                  ↘ FAILED         ↘ NEEDS_HUMAN
```

- `IMPLEMENTED` means the worker exited successfully, left a clean task
  worktree, and left committed changes on the task branch relative to the
  base branch. A run normally continues from there to validation and review;
  a task stays `IMPLEMENTED` when an execution is interrupted between the
  worker and the next stage, and a later run validates and reviews that same
  commit - before starting another worker attempt - when it was never
  rejected.
- `APPROVED` means validation passed and the reviewer approved the exact
  validated commit, possibly after repairs. Approval is immediately followed
  by integration: the base branch is fast-forwarded to that commit and the
  task becomes `DONE`. An `APPROVED` task found by a later run (for example
  after an integration failure or an interruption) skips the worker,
  validation, and review and only makes the integration another attempt.
- `DONE` means the task's approved commit is part of the configured base
  branch. It is recorded only after the fast-forward merge succeeded, is
  never recorded on an integration failure, and satisfies the task's
  dependents. A `DONE` task is not executed again.
- `NEEDS_HUMAN` means all three worker attempts were used without an
  approval: the reviewer still reported critical or major findings, or a
  validation command kept failing. The findings, logs, branch, and worktree
  are preserved and no further attempt is started automatically; re-running
  the task is refused until a human intervenes (removing
  `.actl/attempts/t1` grants a deliberate fresh three-attempt budget).
- `NEEDS_CHANGES` remains from Stage 5: a task left in that state by an older
  actl continues from its remaining attempts - its first worker attempt and
  the stored rejection count against the budget, so its next run starts at
  attempt 2 with the stored findings as feedback.
- `FAILED` covers infrastructure failures: a worker or OpenCode execution
  error, a reviewer execution or verdict-format error, a corrupt attempt
  record, or a validation command that changed tracked files or `HEAD`. None
  of them launch a repair worker, because another implementation attempt
  cannot fix an orchestration problem. A `FAILED` task can be retried
  explicitly; when its last worker attempt had completed, the retry
  validates and reviews that same committed implementation again instead of
  launching another worker, so reviewer failures never spend an
  implementation attempt.
- `actl task run` requires a reviewer model. Without one it refuses before
  taking the execution lock, creating a worktree, or changing the task. It
  also requires an escalation worker model; the worker model is needed
  whenever the worker runs, and an `IMPLEMENTED` or `APPROVED` task is
  finished with the other models alone.
- A task that fails can be retried explicitly with `actl task run <id>`: the
  existing branch and worktree are reused as they are, so earlier commits are
  preserved and the worker continues from there. Nothing is reset, cleaned,
  rebased, or merged, except that an approved task is fast-forwarded into the
  base branch as described above. An explicit retry continues the recorded
  attempt accounting; it never silently restarts the budget. A worker attempt
  counts only once it completes, so an interrupted attempt is retried under
  the same attempt number and does not consume the budget.
- A task may run only when all its dependencies are `DONE`; `APPROVED` does
  not satisfy them. `actl task run` refuses a task with unfinished
  dependencies, and `--all` never executes one (it reports the blocked task
  and its dependencies instead).
- An `APPROVED` task can be run again, but only to integrate its already
  approved commit - the worker, validation, and review are skipped. A `DONE`
  task is never run again.
- If an execution is interrupted (for example by Ctrl-C or a crash), the task
  keeps its status and its worktree and log are preserved; there is no
  automatic recovery, but an explicit `actl task run <id>` continues from the
  preserved worktree and the recorded attempts. An interrupted worker attempt
  is retried under the same attempt number and does not count against the
  budget. An interruption between the successful merge and recording `DONE`
  leaves the task `APPROVED`; the retry detects the completed merge and
  records `DONE` without rerunning anything - actl never blindly repeats Git
  operations or marks an uncertain task `DONE`.
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
edited directly as well). `actl task run` requires it whenever the worker
runs: a task that is not `IMPLEMENTED` fails before the worktree is prepared
or anything is launched without one, while an `IMPLEMENTED` task is validated
and reviewed without it. OpenCode fails the run if the model is not available
in the worktree.

The escalation worker model is project configuration required for every run:

```sh
actl init --escalation-worker-model openrouter/anthropic/claude-opus-latest
```

It is stored as `escalation_worker_model` in `.actl/project.json` and is used
for the third and final worker attempt of a repair cycle. `actl task run`
requires it exactly like the reviewer model: without one the run fails before
taking the execution lock, creating a worktree, or changing the task, instead
of discovering at the last attempt that no stronger model is available. It is
never used for the first two attempts, and the reviewer model never changes
during repairs.

The reviewer model is configured independently:

```sh
actl init --reviewer-model openrouter/anthropic/claude-sonnet-latest
```

It is stored as `reviewer_model` in `.actl/project.json`. `actl task run`
requires it: without a reviewer model the command fails before taking the
execution lock, creating a worktree, or changing the task, instead of
silently skipping the independent review. An `IMPLEMENTED` task is validated
and reviewed without launching the worker again.

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
configured base branch, and approved commits are fast-forwarded back into
it after review; `actl task run` refuses to run while that branch
has no commits - create an initial commit first, since `actl init` never
commits. Re-running `actl init` without `--base` never changes an existing
selection; a project initialized before `base_branch` existed gains one on
the next `actl init`, and `actl task run` keeps working by detecting the
branch without writing metadata.

Every worker attempt writes its own log: `.actl/runs/t1-<timestamp>.log` for
the worker, plus `.actl/runs/t1-<timestamp>-validation.log` and
`.actl/runs/t1-<timestamp>-review.log` when those stages run. Each log starts
with a header (task, attempt, model, branch, commit) and ends with the result
(exit codes, status, verdict, failure reason), and an earlier attempt's logs
are never overwritten. The logs are written continuously while the stage
runs, and each path is printed when the stage starts as well as in the final
summary; use `tail -f` to watch a run.

Only one execution may run per project. The lock is an exclusive, nonblocking
`fcntl.flock` on `.actl/execution.lock`, and the locked file descriptor is
passed to the OpenCode worker and reviewer (Python's `pass_fds`), so the lock
stays held while those processes are alive even if actl itself is killed. A
multi-task run holds that one lock for the whole batch - it is never released
between tasks - so a concurrent `actl task run` recognizes the active
execution immediately. Planner operations (`actl plan`, `actl task create`,
...) stay separate from execution and never take the lock.

## Repair and escalation

A validation command that exits non-zero and a reviewer verdict of
`changes_requested` (which always carries at least one critical or major
finding) are implementation failures: actl feeds them back to the next
worker attempt instead of stopping. A task gets at most three completed
worker attempts:

1. Attempt 1 uses `worker_model`.
2. Attempt 2 uses `worker_model` with the failure feedback.
3. Attempt 3 uses `escalation_worker_model` with the same feedback.

The feedback carries the original task requirements plus either the failed
validation command with the tail of its output or every critical and major
finding with its description, failure scenario, impact, and location. The
worker is told to continue from the existing worktree and commits; the branch
is never reset, rebased, or recreated, and the reviewer always evaluates the
complete cumulative task diff - never just the latest repair commit. Minor
findings and suggestions never trigger a repair: an approval with minor
findings is a successful result, and the reviewer is never asked to become
stricter on later attempts.

Only completed attempts count. A worker or reviewer execution error, a
malformed verdict, or an interrupted attempt does not consume the budget and
never starts a repair: the run stops with `FAILED`, and an explicit retry
re-validates and re-reviews the committed implementation without launching a
worker. When all three attempts have been rejected or have failed validation,
the task ends `NEEDS_HUMAN`.

The console reports each attempt with its model and outcome:

```text
task t1: Add authentication

[14:00] Worker attempt 1/3 (provider/model)
[14:03] Validation passed
[14:04] Reviewer started
[14:05] Review requested changes (2 major findings)

[14:05] Worker attempt 2/3 (provider/model)
[14:08] Validation passed
[14:08] Reviewer started
[14:09] Review requested changes (1 major finding)

[14:09] Worker attempt 3/3 (escalation/model)
[14:12] Validation passed
[14:12] Reviewer started
[14:13] Review approved (no findings)
[14:13] Fast-forward merging...

Task t1 → DONE
```

Approval is immediately followed by the integration of the approved commit
(see "Automatic integration" above); the `Task t1 → APPROVED` line appears
instead when the merge was refused, and the task then keeps its approved
state for a later retry. A `--all` or multi-id run wraps the same events and
ends with a summary such as `Summary: completed: t1, t2`.

Attempt accounting lives in `.actl/attempts/t1`, a small JSON record with the
number of used attempts and the feedback for the next worker, so restarting
`actl` never silently resets the budget. When all three attempts are used the
task ends `NEEDS_HUMAN`; re-running it is refused until a human intervenes.
Deleting `.actl/attempts/t1` is the explicit intervention that grants a fresh
three-attempt budget, and the preserved branch and worktree are reused.

## Validation and review

Validation commands are project configuration in `.actl/project.json`:

```json
{
  "validation": {
    "commands": ["pytest", "ruff check ."]
  }
}
```

- Commands run sequentially in the task worktree, exactly as written (a
  shell), and stop at the first non-zero exit.
- Output, exit codes, and the overall result are recorded in the validation
  log. A failed command with attempts left triggers a repair attempt with the
  command and its output as feedback; with no attempts left the task ends
  `NEEDS_HUMAN`, and no reviewer starts for that attempt.
- After every successful command, actl immediately re-checks the recorded
  commit and the tracked files. A command that modifies tracked files or
  moves `HEAD` fails the task (`FAILED`) right there and is never repaired by
  another worker, because the cause is the validation command rather than the
  implementation; a later command cannot validate the modified state and
  restore the original before a final check.
- A malformed `validation` configuration is an error rather than a silent
  skip; a missing configuration (or an empty list) skips validation and
  reports that clearly before review proceeds.

Validation and review refer to one exact commit:

- The commit is recorded before validation runs and stored in both logs.
- The task worktree must be clean (no uncommitted tracked changes) and on its
  task branch.
- If validation modifies tracked files or moves `HEAD`, the task fails
  immediately instead of being approved; that failure is never repaired
  automatically.
- Before any verdict is recorded, the task branch must still point at the
  validated commit; if it moved, the run stops and the task fails. actl never
  repairs or resets the branch.

The commit a task branch was created from is recorded in `.actl/base/t1` when
the branch is created, so the review diff always starts at the original base
even if the project's base branch is reconfigured later. Branches created
before this record existed (Stage 4 projects) fall back to the merge base
with the configured base branch.

After validation passes, the reviewer runs in a temporary detached worktree
under `.actl/review/t1-<timestamp>` created at the validated commit, never in
the task worktree. It is launched with the project's reviewer model and
OpenCode's built-in read-only `plan` agent:

```sh
opencode run --agent plan --model REVIEWER_MODEL --session ID --format json "<prompt>"
```

The reviewer receives the task title and description (requirements and
acceptance criteria), the cumulative diff of the task branch against the
commit it was created from, the validation results, and the review policy. It
can inspect the whole checkout for context. Before its verdict is accepted,
the reviewer's checkout must still point at the validated commit with
unchanged tracked files (untracked scratch files are fine); otherwise the
review fails. Its temporary worktree is removed afterwards on every path, so
nothing it does can reach the task worktree or the task branch.

The reviewer must answer with one JSON verdict:

```json
{"decision": "approved", "findings": []}
```

```json
{
  "decision": "changes_requested",
  "findings": [
    {
      "severity": "major",
      "description": "Authentication accepts an expired token.",
      "scenario": "An expired JWT is accepted by the verification function.",
      "impact": "Users can authenticate with expired credentials.",
      "location": "src/auth.py"
    }
  ]
}
```

The verdict is parsed strictly. The reviewer's final message must be exactly
one complete JSON object - a single surrounding Markdown code fence is
tolerated, but prose, several objects, and truncated JSON are refused.
Malformed output, unknown decisions or severities, an approval that carries a
critical or major finding, and a `changes_requested` verdict without a
blocking finding are all invalid: the task becomes `FAILED` and nothing is
recorded as an approval. Every critical or major finding must include a
scenario, impact, and location. Valid verdicts are stored in `.actl/tasks.db`
(the `reviews` table) with the commit they refer to, and Stage 6 feeds the
critical and major findings back to the next worker attempt. Reviewer
execution and format failures are recorded in the review log with a reason;
they fail the task and never trigger a repair worker.

The review policy is the built-in risk-based policy plus, when present, the
project's `REVIEW_POLICY.md`:

- The reviewer judges whether the task satisfies its requirements, whether it
  introduces significant correctness problems, and whether it creates
  credible security, safety, or data-loss risks.
- Only **critical** or **major** findings reject a task. **Minor** findings
  and **suggestions** are reported but never block approval.
- Blocking findings need a concrete failure scenario, a realistic impact, why
  they block, and the relevant files; the reviewer weighs likelihood and
  impact.
- Style preferences, speculative improvements, extremely unlikely edge cases,
  nonessential robustness work, features from future stages, and unrelated
  pre-existing problems never block approval.

`APPROVED` records the validated commit and is immediately followed by the
integration described above: the task ends `DONE` once the base branch has
fast-forwarded to that commit, and keeps `APPROVED` when the integration was
refused. Nothing is ever pushed to a remote.

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
| Create a reviewer session in a temporary worktree | `opencode api POST /api/session` (location and agent `plan`) |
| Run the reviewer and get a machine-readable verdict | `opencode run --agent plan --model <model> --session <id> --format json "<prompt>"` |

Sessions are created explicitly (instead of letting the TUI create them
lazily) so actl captures the real session ID. Before resuming, actl asks
OpenCode for the session and compares its `location.directory` with the
project root; if the session is missing, unreadable, or belongs elsewhere,
actl reports an error instead of resuming the wrong session.

## Project files

```
.actl/
├── project.json    # schema_version, name, worker_model, reviewer_model,
│                   # escalation_worker_model, validation.commands,
│                   # base_branch, created_at
├── planner.json    # active planner session id, created_at
├── tasks.db        # SQLite task database (tasks, dependencies, reviews)
├── execution.lock  # project-wide execution lock (fcntl.flock)
├── base/           # branch-creation commit per task: t1, t2, ...
├── attempts/       # used worker attempts + next-attempt feedback: t1, t2, ...
├── runs/           # tN-<timestamp>.log worker, -validation.log, -review.log
├── review/         # temporary reviewer worktrees (removed after review)
├── integrate/      # temporary merge worktrees (removed after the merge)
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
without a stored `base_branch`. The Stage 5 tests cover validation success,
failure, per-command worktree tampering and `HEAD` movement, modify-and-
restore command sequences, skipped validation, every verdict shape
(approval with and without findings, rejection, malformed, truncated and
prose-wrapped output, unknown severities and decisions, contradictory
verdicts), reviewer isolation, checkout mutation and the temporary
worktree, a task branch moving during review, the built-in and project
review policies, the configured models, the missing-reviewer-model preflight
and its ordering before the execution lock, the recorded branch base, and
validating/reviewing an `IMPLEMENTED` task without rerunning the worker or
requiring a worker model. The Stage 6 tests cover the repair loop: approval on
the first attempt, minor findings without a retry, a second attempt with
reviewer feedback, escalation on the third attempt, `NEEDS_HUMAN` after three
rejections or three validation failures, validation feedback, cumulative
review diffs, preserved worktrees and commits, separate per-attempt logs,
reviewer failures that never trigger a repair and never spend a worker
attempt on an explicit retry, the persisted attempt budget across invocations
(including an interrupted run and legacy `NEEDS_CHANGES` tasks), the explicit
reset path, the required escalation model and its preflight ordering before
the execution lock, and the escalation model configuration. The Stage 7
tests cover integration and sequential execution: an approved task merging
and becoming `DONE`, `DONE` only after a successful merge, the configured
base branch (including one that is not checked out anywhere), a dirty base
worktree, a non-fast-forward base, a task branch that moved after approval,
an already approved task integrating without rerunning any agent, an
already `DONE` task refusing to run, a completed merge whose `DONE` record
was lost, dependencies unlocking only after `DONE`, explicit task lists,
`--all` in dependency order, deterministic id order for independent tasks,
stopping at the first failure or at a task that needs a human, never
executing a task with unfinished dependencies, an ignored local file the
merge must never overwrite, branch squash options that cannot fake a
successful integration, and one execution lock per batch.

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
- Stage 7 executes one task at a time - never in parallel - and stops a
  multi-task run at the first task that cannot complete. A task gets
  at most three completed worker attempts, the third with the escalation
  model; after that it is `NEEDS_HUMAN` and only explicit human intervention
  continues. There is no automatic rebasing, no conflict resolution, no pull
  requests or GitHub/GitLab integration, and no automatic crash recovery.
- `IMPLEMENTED` only means that the worker exited successfully, left the
  worktree clean, and left committed changes on the task branch relative to
  the base branch. `APPROVED` additionally means the configured validation
  commands passed on that exact commit and the reviewer approved it, possibly
  after repairs; `DONE` then means that exact commit was fast-forwarded into
  the base branch. None of these states is a guarantee of correctness.
- `actl task run` requires a reviewer model and an escalation worker model
  for every run, and a worker model whenever the worker runs. Without any of
  them the run fails before launching anything; an `IMPLEMENTED` task is
  validated and reviewed with just the reviewer and escalation models, and
  an `APPROVED` task is integrated the same way, without rerunning the
  worker.
- A validation command that changes tracked files or moves `HEAD` fails the
  task immediately and is never repaired by another worker: the cause is the
  validation command, not the implementation. actl reports it instead of
  burning attempts on it.
- A corrupt `.actl/attempts/tN` record fails the task instead of silently
  resetting the repair budget; fix or remove the record deliberately.
- Validation commands run with actl's environment inside the task worktree
  and may create untracked files (caches, reports). Changes to tracked files
  or to `HEAD` fail the task as soon as a command finishes; actl does not
  clean anything up.
- actl never resets, cleans, rebases, or deletes a task worktree. A task that
  fails keeps its branch, worktree, and log for an explicit retry. Two kinds
  of worktree are temporary and removed again: the reviewer's checkout and
  the merge worktree used when no worktree has the base branch checked out.
- Integration is a plain `git merge --ff-only` of the exact approved commit;
  `--no-squash` and `--no-overwrite-ignore` keep branch configuration from
  staging a squash and from replacing an ignored local file. actl never
  rewrites the base branch: when the base branch advanced with its own
  commits, the merge is refused and a human must reconcile it by hand.
  Merging also refuses while an uncommitted or untracked file sits in the
  worktree that has the base branch checked out, instead of touching the
  user's work; every refusal keeps the task `APPROVED` for a later retry.
  When no worktree has the base branch checked out, a temporary worktree
  performs the merge, so that checkout does not see the new files until it
  is updated.
- `actl task run --all` does not select `DONE` or `NEEDS_HUMAN` tasks. It
  reports the tasks that still need a human and stops when no selected task
  can progress, so a project with an abandoned `NEEDS_HUMAN` task can still
  finish and report success for everything else that was runnable.
- The execution lock covers actl and the OpenCode processes it spawns. A
  detached descendant that does not keep the inherited descriptor (for example
  a daemonized helper the worker starts) can outlive the lock; actl keeps no
  PID tracking, heartbeats, or automatic recovery.
- The worker runs with OpenCode's standard `build` agent, so it may modify
  anything inside its worktree. A noninteractive run cannot ask for
  permissions: a request that would prompt in the TUI (for example reading a
  `.env` file or touching a path outside the worktree) is auto-rejected and
  the worker continues without it. actl does not pass `--auto`, so those
  guards stay active. Like the planner, this is not a sandbox - an approved
  task is merged automatically, so review the resulting base-branch history
  and commits instead of trusting the worker.
- The worker prompt asks the worker to commit everything and never merge into
  the base branch, and actl checks afterwards that the worktree is still on
  its task branch, is clean, and contains committed changes relative to the
  base branch; a worker that ignores the instructions is reported as a
  failure rather than corrected automatically.
- The reviewer runs with OpenCode's read-only `plan` agent in a disposable
  worktree at the validated commit; it can still run shell commands, so treat
  its transcript as untrusted output. Its checkout must still be at the
  validated commit with unchanged tracked files when it answers, and its
  final message must be one complete JSON object (a single code fence is
  tolerated). Malformed, truncated, or contradictory answers always fail the
  task instead of being approved.
- The review verdict is bound to the validated commit, but it is a model
  judgment: minor findings may be missed, and an approval is not a
  correctness proof. Because an approval immediately fast-forwards that
  commit into the base branch, review what the worker produced with
  `git log`/`git diff` on the base branch as well; actl never pushes, so a
  wrong merge can still be corrected locally.
