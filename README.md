# Josi

Josi is a lightweight Python CLI for orchestrating AI coding agents through
[OpenCode](https://opencode.ai), [Claude Code](https://claude.com/claude-code), or Codex
(a per-project choice, see "Agent backends"). Stage 1 covers project setup and planning;
Stage 2 adds a project-local task list; Stage 3 lets the planner manage that
task list through the same `josi task` CLI; Stage 4 runs one task at a time
with a noninteractive OpenCode worker in an isolated Git worktree; Stage 5
validates the worker's commit with project-configured commands and has an
independent reviewer approve or reject it; Stage 6 repairs rejected
implementations automatically with up to three worker attempts and escalates
to a stronger model on the last one; Stage 7 executes several tasks
sequentially inside one **run**: every approved task is staged into the run
branch, and one explicit `josi run merge` fast-forwards the whole run into the
configured base branch and marks its tasks `DONE`.

## Requirements

- Python 3.9+
- Git
- The CLI of the project's agent on `PATH`: OpenCode (v2, tested with OpenCode
  2.1.2) for the default `opencode` agent, or Claude Code (`claude`) for the
  `claude` agent, or Codex (`codex`) for the `codex` agent

## Install

```sh
pip install -e .
# or run without installing:
PYTHONPATH=src python -m josi --help
```

## Usage

```sh
cd your-project
josi init                   # register the Git repository as a Josi project
josi plan                   # start a fresh planner session; it can manage tasks
josi task list              # list the project's tasks
josi task run t1            # run a task: tests, worker, review, staging
josi task run t1 t2 t3      # run tasks sequentially in the given order
josi task run --all         # run every eligible task in dependency order
josi task review t1         # validate and review a hand-fixed task, then stage it
josi run list               # list runs and their statuses
josi run show r1            # show one run, its commits, and its tasks
josi run merge r1           # integrate a completed run into the base branch
```

- `josi init` requires a Git repository, creates `.josi/project.json`, and adds
  `.josi/` to `.gitignore` (existing `.gitignore` content is preserved).
  Running it again is harmless. It also checks that OpenCode is installed.
  It always records all four model settings, so a project is ready to run a
  task right away. Without an argument each setting gets a default:
  `worker_model` = `openrouter/deepseek/deepseek-v4.1-flash#low`,
  `reviewer_model` = `openrouter/openai/gpt-6-luna#high`,
  `escalation_worker_model` = `openrouter/anthropic/claude-sonnet-5.5#medium`,
  and `test_writer_model` = `openrouter/z-ai/glm-5.3#high`.
  `josi init --worker-model MODEL` overrides the default worker model, for
  example `josi init --worker-model openrouter/anthropic/claude-sonnet-latest`;
  `josi init --reviewer-model MODEL` overrides the independent reviewer's
  model, which `josi task run` requires; and
  `josi init --escalation-worker-model MODEL` overrides the stronger model used
  for the third and final repair attempt, which `josi task run` also requires;
  and `josi init --test-writer-model MODEL` overrides the project's test-writer
  model, required by `josi task run` like the reviewer and escalation models.
  Re-running `josi init` preserves every model already stored and fills in a
  default only for a setting that is missing; passing an argument updates that
  one setting and leaves the others untouched. It never configures validation
  commands: validation is optional project configuration (see
  "Validation and review").
  `josi init --base BRANCH` sets the base branch that task branches are
  created from; without it the branch is detected, and when the choice is
   ambiguous choice, Josi asks. It works in repositories without commits, where it
  records the unborn `HEAD` branch and never creates a commit itself.
- `josi plan` opens the interactive OpenCode TUI from the project root and
  waits for your input - it never sends an initial message. The planner
  instructions live in a dedicated OpenCode custom agent: editing, subagents,
  web access, and MCP tools are denied, and shell commands ask for approval
  except the `josi task` management commands, which are pre-approved
  (`josi task run` stays denied). Model selection stays interactive inside
  OpenCode: Josi never passes `--model`.
- **Every `josi plan` run starts a fresh planner session**; Josi stores no
  planner session ID and never resumes one. Earlier sessions stay available
  inside the agent CLI.

## Agent backends

A project runs on one agent CLI, stored as `agent` in `.josi/project.json`:
`opencode` (the default, also assumed for projects created before the setting
existed), `claude`, or `codex`. Every role - planner, worker, reviewer, escalation
worker, and test writer - uses it.

```sh
josi init                      # OpenCode (default)
josi init --agent claude       # Claude Code
josi init --agent codex        # Codex
```

Model settings are `(model, effort)` pairs, stored as
`{"model": "sonnet", "effort": "low"}` and given on the command line as
`name#effort` (the effort is optional). For OpenCode Josi joins the pair back
into `provider/model#variant`; for Claude Code it passes `--model name` and
`--effort effort` (`low`, `medium`, `high`, `xhigh`, `max`). Each agent has
its own defaults:

| setting | `opencode` | `claude` | `codex` |
| --- | --- | --- | --- |
| worker | `openrouter/deepseek/deepseek-v4.1-flash#low` | `sonnet#low` | `gpt-6.1-sol#low` |
| reviewer | `openrouter/openai/gpt-6-luna#high` | `opus#medium` | `gpt-6.1-sol#medium` |
| escalation worker | `openrouter/anthropic/claude-sonnet-5.5#medium` | `opus#medium` | `gpt-6-astra#medium` |
| test writer | `openrouter/z-ai/glm-5.3#high` | `sonnet#medium` | `gpt-6.1-sol#medium` |

`josi init --agent` on an existing project switches its agent and resets all
four model settings to the new agent's defaults (models named in the same
command are kept). Re-running `josi init` without `--agent` keeps the stored
agent and models. Older projects that store models as `name#effort` strings
keep working unchanged.

With `codex`:

- Install the Codex CLI with its `codex` executable on `PATH` and authenticate
  separately (for example, `codex login`). Your account must have access to the
  configured models; Josi does not substitute models automatically or use an SDK.
- Model names need no provider prefix. Josi passes `--model` and configures
  `model_reasoning_effort` for `#none`, `#minimal`, `#low`, `#medium`, `#high`,
  or `#xhigh`. Codex reports model-specific effort incompatibilities.
- Workers, escalation workers, and test writers use `codex exec --json` with
  prompts on stdin and `--dangerously-bypass-approvals-and-sandbox`. They can
  execute commands without confirmation or sandbox restrictions, matching
  Josi's Claude automation behavior. Use a trusted repository and environment.
- Reviewers use a read-only sandbox with approvals disabled. Format retries
  resume the exact thread reported by `thread.started`, with the same model,
  effort, and permissions. Logs include the real Codex thread ID. Josi retains
  its checkout-integrity checks.
- `josi plan` launches a fresh interactive session at the project root, with
  no initial message or forced model. Planner instructions use
  `developer_instructions`; Josi does not modify `AGENTS.md`. The planner uses
  a read-only sandbox with `on-request` approvals. Josi installs managed command
  [Codex command rules](https://learn.chatgpt.com/docs/agent-configuration/rules)
  in `.codex/rules/josi-planner.rules` that pre-approve only
  `josi task create/list/show/update/delete` outside the sandbox, allowing task
  database writes without repeated approval prompts. Source files remain
  read-only for ordinary commands; no directory is made writable. Read-only
  inspection does not require confirmation. Instructions prohibit implementation, task execution,
  web access, and delegation; native web search and multi-agent features are
  disabled for the invocation. Task execution/reset commands are not pre-approved.
  Codex must trust the project before it loads project-local rules; its normal
  trust confirmation may appear once. Josi creates `.codex/config.toml` if missing
  and preserves existing configuration and other rules. The managed allow rules
  remain available to other Codex sessions in this project. User/admin rules may
  impose additional prompts or restrictions. Allow rules run matched commands
  outside the sandbox, so use a trusted `josi` executable. Other behavioral
  restrictions are instructions, with weaker command-level enforcement than
  the OpenCode planner's permission rules.
- Normal Codex authentication and configuration are inherited; explicit Josi
  settings take precedence. Input, cached-input, and output tokens come from
  `turn.completed`; cached tokens are separated from input to avoid double
  counting. Unreported usage fields and dollar costs remain unavailable.

With `claude`:

- `josi plan` runs interactive `claude` from the project root with the planner
  instructions appended to its system prompt. Editing, web access, subagents,
  and MCP servers are disallowed, `josi task create|list|show|update|delete`
  are pre-approved, and `josi task run`, `josi task reset`, and `josi run` are
  disallowed. This is best-effort: Claude Code matches shell permissions by
  command prefix. Model selection stays interactive.
- Workers, the test writer, and the reviewer run as `claude -p` with the
  prompt on stdin and `--permission-mode bypassPermissions`; the isolated task
  worktree is the boundary. The reviewer additionally disallows the editing
  and web tools. Each turn's answer, tokens, and cost come from the final
  `result` event of `--output-format stream-json`.
- Usage is recorded best-effort from that event: token counts and the cost
  Claude Code reports. The input/output cost split is not reported, so Claude
  totals are shown as `incomplete`.
- A session is a UUID chosen by Josi; a format retry resumes it with
  `--resume`.

## Tasks

Stage 2 stores the project's tasks in a SQLite database (`.josi/tasks.db`).
Task commands are noninteractive: everything comes from arguments and files,
output is plain text, and problems are reported on stderr with a non-zero
exit code.

The planner manages the same task list with the same commands: the
`josi task` management commands are the only shell commands it may run
without a permission prompt, so task planning and task management happen in
one OpenCode session. (`josi task run` is denied to the planner.)

```sh
josi task create --title "Add the login form" --depends-on t1
josi task create --title "Write the login tests" --description-file tests.md
josi task list
josi task show t1
josi task update t2 --title "New title" --depends-on t1 --depends-on t3
josi task delete t2
josi task reset t2
```

- Tasks have an integer id, printed as `t1`, `t2`, ...; commands accept both
  `t1` and `1`. New tasks are `PENDING`; `josi task run` is the only command
  that advances a task's status (`josi task reset` deliberately returns one to
  `PENDING`).
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
  `josi task run` refuses blocked tasks. A dependency is satisfied by `DONE`
  (its approved commit was integrated into the base branch) or by `STAGED` in
  the run the dependent is currently executing in (its approved commit is
  already part of the same run branch). `APPROVED` and `STAGED` in another
  run do not unlock dependents.
- `josi task review tN` is the way back after you fixed a `NEEDS_HUMAN` task by
  hand (commit the fix on the task branch first). It runs no test generation
  and no worker: it validates and reviews the commit already on the task
  branch. On approval the task is staged exactly as by `josi task run`; if
  the reviewer still requests changes, the findings are reported and the task
  stays `NEEDS_HUMAN`. It also accepts `FAILED` and `IMPLEMENTED` tasks, takes
  the execution lock, and neither spends nor refunds worker attempts.
- `josi task reset tN` starts a task over: it clears the task's worker attempt
  budget (`.josi/attempts/tN`), the recorded test-generation outcome
  (`.josi/testgen/tN`), returns its status to `PENDING`, clears its run
  association, deletes its stored reviews, and discards its task branch and
  worktree (including any uncommitted work), so the next execution creates a
  fresh branch from the run branch like a new task and runs test generation
  again. Dependencies, title, and description are preserved. The worktree is
  removed before any state is discarded, so a failed cleanup leaves the task
  untouched and the reset can be retried. It is an explicit human intervention
  - not a planning command - so it takes the project-wide execution lock and
  refuses while an execution is running. A `STAGED` task cannot be reset: its
  approved commit is already part of a run branch and cannot be rewritten, so
  integrate the run with `josi run merge` first. To grant a fresh budget while
  keeping the existing commits, remove `.josi/attempts/tN` by hand instead.
- The database is created automatically when needed, including for projects
  initialized by Stage 1. Repeated `josi init` runs and task commands never
  reset existing tasks.
- Task commands never touch another project's database: they resolve the Git
  repository root first and require `.josi/` there, so a nested repository is
  never served from its parent's database. Only `josi task run` invokes
  OpenCode.

## Running tasks

`josi task run` executes tasks end to end inside a **run**: a
test-generation turn writes the task's tests first, a noninteractive OpenCode
worker implements each task in an isolated Git worktree, the project's
validation commands run on the worker's commit, an independent reviewer
approves or rejects that exact commit, and the approved commit is
fast-forwarded into the run branch, which marks the task `STAGED`. The
configured base branch is never touched during execution; `josi run merge`
integrates the whole run later and marks its tasks `DONE`. A failed validation
command or a reviewer rejection with critical or major findings triggers an
automatic repair: Josi runs up to three worker attempts in all and escalates
the last one to a stronger model.

```sh
josi task run t1          # one task: tests, worker, validation, review, staging
josi task run t1 t2 t3    # the listed tasks, sequentially, in that order
josi task run --all       # every eligible task, in dependency order
josi run merge r1         # integrate the completed run into the base branch
```

One run goes through these steps:

1. The project must be initialized with a reviewer model, an escalation
   worker model, and a test-writer model, and the task must exist, be
   runnable, and have all its dependencies satisfied (`DONE`, or `STAGED` in
   the run being executed). A task that starts the worker also needs the
   worker model; without one of the required models the command refuses
   before any lock, worktree, or task change.
2. The project-wide execution lock is taken. A second `josi task run` in the
   same project refuses immediately while an execution is active, whether it
   is another single task or a multi-task batch.
3. The batch creates one run before its first task executes: branch
   `josi/run/r1` and worktree `.josi/run-worktrees/r1`, starting at the
   current commit of the project's configured `base_branch`. An interrupted
   or stopped execution that left its run `RUNNING` is resumed instead, so
   staged tasks stay in the same run. Only one run executes at a time, the
   same run serves every task of the batch, and the run records the exact
   commit its branch is expected to point at: the start commit, advanced
   only by successfully staged, reviewer-approved tasks.
4. Each task gets a dedicated branch (`josi/t1`) and worktree
   (`.josi/worktrees/t1`) created from the **run branch**, never directly
   from the base branch, so a task starts from the previous staged task's
   result. They are created the first time the task runs in this run - never
   at `josi task create`.
5. Before the first worker attempt, a test-generation turn runs in the task
   worktree with the project's test-writer model and the built-in `build`
   agent. It decides whether the task needs automated tests: it either
   writes and commits them on the task branch, or explicitly decides that no
   tests are needed and changes nothing. A final message that is not one
   JSON object is reformatted once, like the reviewer's verdict. The
   generated tests stay committed on the task branch: the worker implements
   the task around them, and the project's configured validation commands
   exercise them after the worker - Josi never runs the tests itself, and
   validation stays optional when no commands are configured. A completed
   stage is recorded (and never repeated on a retry); an execution error, an
   invalid outcome, uncommitted changes, or a dirty worktree fails the task
   instead of starting the worker.
6. OpenCode runs one noninteractive turn inside the worktree with the
   project's worker model and the built-in `build` agent
   (`opencode run --agent build --model MODEL --session ID`). A repair
   attempt uses the escalation model on the third attempt and carries the
   previous failure as feedback; the worker continues on the same branch and
   Josi never resets the branch or discards earlier commits.
7. The worker's complete stdout and stderr are written straight into a log
   file under `.josi/runs/` while it runs; the log path is printed when the
   worker starts, so the run can be followed with `tail -f <logfile>`.
8. After the worker succeeds, the configured validation commands run in the
   task worktree (see below) and an independent reviewer examines the
   validated commit in a temporary worktree.
9. An approval marks the task `APPROVED` and Josi stages it: the exact
   validated and approved commit is fast-forwarded into the run branch (see
   below) and the task becomes `STAGED`. A failed validation command or a
   rejection with critical or major findings starts the next worker attempt
   in the same branch and worktree, with the failure as feedback; when all
   three attempts are used the task ends `NEEDS_HUMAN`.
10. When the batch ends and every task selected for the run is staged (or
    already DONE), and the run branch still points at the expected commit, the
    run is marked `COMPLETE` with that verified commit as its final commit. A
    batch stopped by a failed task, a blocked `--all`, or a crash leaves the
    run `RUNNING` and resumable, and is never reported as finished.
11. Only after the run is integrated into the base branch with
    `josi run merge` is a staged task marked `DONE`. DONE means the task's
    approved commit is part of the configured base branch; it is never
    recorded while the task is merely staged, and a task that is already DONE
    is not executed again.

`josi task run t1` after an earlier approval does not repeat any of the
expensive work either: when the task is already `APPROVED`, Josi skips the
worker, validation, and review and attempts to stage the recorded approved
commit into the run.

The terminal shows only Josi's own timestamped events - task, models,
worktree preparation, test-writer start and outcome, worker start, worker
exit code, validation start and result, reviewer start and verdict, the
staging merge, and the final task status - plus the log paths and the final
reminder with the `josi run merge` command. Transcripts and validation output
never reach the terminal. A multi-task run prints a final summary of the
completed tasks and of the task that stopped it, if any.

### Staging into the run branch

Reaching `APPROVED` immediately stages the task into its run branch. Josi
merges only the exact commit the reviewer approved:

- The approved commit comes from the recorded review. The task branch must
  still point at it; if the branch moved, nothing is merged.
- The run branch must exist, and its worktree must be clean (no uncommitted
  or untracked changes).
- The merge is `git merge --ff-only --no-squash --no-overwrite-ignore`:
  Josi never force-pushes, resets, rebases, or squashes. When a fast-forward
  is impossible, Josi stops and reports the problem instead of changing
  anything. After the merge, the run branch must really contain the approved
  commit before the task is reported as staged.
- A staging failure keeps the task `APPROVED`, so its branch, worktree, and
  approval are preserved. Resolving the problem and rerunning
  `josi task run <id>` stages the same approved commit without rerunning
  the worker, validation, or review.
- If the staging merge succeeds but recording `STAGED` fails (for example
  because Josi is interrupted in between), the task stays `APPROVED`; the
  retry recognizes the completed merge and only records `STAGED`. The worker
  is never rerun and no new implementation is created.

### Integrating a run

`josi run merge r1` fast-forwards a completed run into the configured base
branch and transitions its `STAGED` tasks to `DONE`. The command is
idempotent and detects a manual integration:

- The run must be `COMPLETE`: every task selected for it was staged (or was
  already `DONE`) and the run branch pointed at the expected commit when the
  run finished. A failed batch stays `RUNNING`; resume it with
  `josi task run ...` instead of integrating a partial plan. The recorded
  final commit is the verified expected commit, never whatever the branch
  happened to hold when execution stopped.
- If the recorded final commit is already an ancestor of the base branch -
  the user merged the run by hand, with or without a merge commit, or the
  command is repeated - the Git merge is skipped and only the statuses are
  finalized.
- Otherwise the run branch is fast-forwarded into the base branch with
  `git merge --ff-only --no-squash --no-overwrite-ignore`, in the worktree
  that has the base branch checked out (or a temporary worktree under
  `.josi/integrate/` when none has it). The base branch must be clean, and
  an ignored local file is never silently overwritten.
- When a fast-forward is impossible (the base branch advanced with its own
  commits), nothing changes: the run stays `COMPLETE`, its tasks stay
  `STAGED`, and the command explains the manual Git integration. After the
  user resolves the merge by hand and commits it, rerunning the same command
  detects the integration and finalizes the statuses.
- Squash merges and cherry-picks are not detected automatically; integrate
  the run with a normal merge or fast-forward.
- STAGED tasks are marked DONE only here. A task that is merely staged is
  never reported as DONE.

```sh
josi run list      # r1  COMPLETE  josi/run/r1  t1, t2
josi run show r1   # status, branch, commits, and each associated task
josi run merge r1  # fast-forward the run, then STAGED -> DONE
```

Runs only ever fast-forward. Josi never merges the run branch automatically:
even a successful batch stops with a reminder such as

```text
[14:05] Run r1 finished with 2 staged tasks; integrate it with 'josi run merge r1'
```

### Multiple tasks

`josi task run t1 t2 t3` executes the listed tasks sequentially in exactly
that order; Josi never reorders the list. Dependencies are checked for each
task when its turn comes, so a task may depend on an earlier task of the
same list - by the time it is reached, that task has become `STAGED` in the
same run. Execution stops immediately at the first task that cannot complete:
the tasks staged before it stay staged in the run branch, and the remaining
listed tasks are left untouched.

`josi task run --all` selects every eligible task and executes them one at a
time in dependency order:

- A task is eligible when its status is one the single-task flow accepts:
  `PENDING`, `RUNNING`, `IMPLEMENTED`, `FAILED`, `NEEDS_CHANGES`, or
  `APPROVED`. `STAGED` tasks are already in a run branch, `DONE` tasks are
  already integrated, and `NEEDS_HUMAN` tasks require a human, so none of
  them is selected automatically.
- Among the tasks whose dependencies are satisfied, the one with the smallest
  id runs first; the task list is re-read after every task, so a staged task
  immediately unlocks its dependents in the same run. Independent tasks
  therefore run in id order.
- A task whose dependencies stay unfinished never executes. When no selected
  task is ready, Josi reports the blocked tasks and their unfinished
  dependencies and stops instead of reporting success.

Any task that fails to complete - a worker or reviewer infrastructure
failure, an exhausted repair budget, human intervention being required, a
refused or failed staging, or an unexpected state - stops the batch
immediately. The run branch, task worktrees, logs, and the already staged
work are preserved; the run stays `RUNNING`, the failing task is reported,
and the command exits non-zero. `josi task run ...` resumes that same run,
so the remaining selected tasks continue from the staged work and the whole
plan finishes in isolation. `josi run merge` refuses an unfinished run: no
partial plan is ever integrated automatically or by accident.

Only one unfinished, unmerged run may exist. A stopped or interrupted run
stays `RUNNING` and the next `josi task run ...` resumes it, so a failed
batch never opens a second run and its staged tasks keep satisfying their
dependents. A `COMPLETE` run awaiting integration does block a new run until
`josi run merge` integrates it, even when its staged tasks have no unfinished
dependents. When a task depends on a task that is `STAGED` in another run,
Josi refuses with the exact command that fixes it:

```text
Cannot execute task t5.

Dependency t3 is STAGED in run r1 and must be
marked DONE before this task can execute.

Run:
    josi run merge r1

If you already merged r1 manually, execute the
same command to finalize its status.
```

The worker prompt contains the task title, its description, and the worker
instructions: implement the task, keep the tests the test-generation stage
wrote unchanged, run the relevant checks, commit the changes on the branch,
keep the worktree clean, and never merge into the run branch.

The test writer receives the task title and description in the task worktree
with its own instructions: decide whether the task needs automated tests, and
either write and commit them (running them to verify they pass) or explicitly
decide that no tests are needed and change nothing. It must end its turn with
exactly one JSON decision - `tests_written` or `no_tests_needed` - which is
parsed strictly like the reviewer's verdict; prose, several objects, and
unknown decisions are refused. A final message that is not one JSON object is
reformatted once by the same session, like the reviewer's verdict: the
correction may only restate the decision, and a turn that changes the branch
fails the stage. `tests_written` counts only together with committed changes,
`no_tests_needed` succeeds without requiring a commit, and either way the
worktree must be clean before the worker starts.

The lifecycle is:

```text
PENDING → RUNNING → IMPLEMENTED → APPROVED → STAGED → DONE
                  ↘ FAILED         ↘ NEEDS_HUMAN
```

- `IMPLEMENTED` means the worker exited successfully, left a clean task
  worktree, and left committed changes on the task branch relative to the
  run branch. A run normally continues from there to validation and review;
  a task stays `IMPLEMENTED` when an execution is interrupted between the
  worker and the next stage, and a later run validates and reviews that same
  commit - before starting another worker attempt - when it was never
  rejected.
- `APPROVED` means validation passed and the reviewer approved the exact
  validated commit, possibly after repairs. Approval is immediately followed
  by staging: the run branch is fast-forwarded to that commit and the task
  becomes `STAGED`. An `APPROVED` task found by a later run (for example
  after a staging failure or an interruption) skips the worker, validation,
  and review and only makes the staging another attempt.
- `STAGED` means the task's approved commit is part of its run branch. It is
  recorded only after the staging fast-forward succeeded, satisfies the
  task's dependents that execute in the same run, and is preserved when a
  batch stops at a failed task. Staging also advances the run's recorded
  expected commit, the only tip the run accepts; a commit made by hand in the
  run worktree is refused, never recorded and never integrated. A STAGED task
  is not executed again; it must be integrated with `josi run merge` to
  become `DONE`.
- `DONE` means the containing run was integrated into the configured base
  branch. It is recorded only by `josi run merge`, after the run branch was
  fast-forwarded (or found already merged) into the base branch; it is never
  recorded while the task is merely staged, and it satisfies dependents in
  any later run. A `DONE` task is not executed again.
- `NEEDS_HUMAN` means all three worker attempts were used without an
  approval: the reviewer still reported critical or major findings, or a
  validation command kept failing. The findings, logs, branch, and worktree
  are preserved and no further attempt is started automatically; re-running
  the task is refused until a human intervenes (removing
  `.josi/attempts/t1` grants a deliberate fresh three-attempt budget, and
  `josi task reset t1` additionally returns the task to `PENDING` and
  discards its branch and worktree).
- `NEEDS_CHANGES` remains from Stage 5: a task left in that state by an older
  Josi continues from its remaining attempts - its first worker attempt and
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
- `josi task run` requires a reviewer model. Without one it refuses before
  taking the execution lock, creating a worktree, or changing the task. It
  also requires an escalation worker model; the worker model is needed
  whenever the worker runs, and an `IMPLEMENTED` or `APPROVED` task is
  finished with the other models alone.
- A task that fails can be retried explicitly with `josi task run <id>`: the
  existing branch and worktree are reused as they are, so earlier commits are
  preserved and the worker continues from there. Nothing is reset, cleaned,
  rebased, or merged, except that an approved task is fast-forwarded into the
  run branch as described above. An explicit retry continues the recorded
  attempt accounting; it never silently restarts the budget. A worker attempt
  counts only once it completes, so an interrupted attempt is retried under
  the same attempt number and does not consume the budget.
- A task may run only when all its dependencies are satisfied: `DONE`, or
  `STAGED` in the run being executed. `APPROVED`, and `STAGED` in another
  run, do not satisfy them. `josi task run` refuses a task with unfinished
  dependencies, and `--all` never executes one (it reports the blocked task
  and its dependencies instead).
- An `APPROVED` task can be run again, but only to stage its already approved
  commit - the worker, validation, and review are skipped. A `STAGED` task
  is not executed again until its run is integrated, and a `DONE` task is
  never run again.
- If an execution is interrupted (for example by Ctrl-C or a crash), the task
  keeps its status and its worktree and log are preserved, and the run stays
  `RUNNING`; there is no automatic recovery, but an explicit `josi task run`
  resumes that run and continues from the preserved worktree and the recorded
  attempts. An interrupted worker attempt is retried under the same attempt
  number and does not count against the budget. An interruption between the
  successful staging merge and recording `STAGED` leaves the task `APPROVED`;
  the retry detects the completed merge and records `STAGED` without
  rerunning anything - Josi never blindly repeats Git operations or marks an
  uncertain task DONE.
- The task is marked `IMPLEMENTED` only when OpenCode exits with status 0,
  the worktree is still on its task branch, the worktree is clean, and the
  branch has committed changes relative to the run branch. A worker that
  leaves uncommitted or untracked files behind, or commits nothing, is a
  failure. A retry may finish with previously committed work and no new
  commit.

The worker model is project-level configuration:

```sh
josi init --worker-model openrouter/anthropic/claude-sonnet-latest
josi init --worker-model provider/model#variant   # with a model variant
josi init --agent claude --worker-model sonnet#low
```

It is stored as `worker_model` in `.josi/project.json` as a model/effort pair
(the file may be edited directly as well) and defaults to the agent's worker
default (see "Agent backends") when `josi init` runs without
`--worker-model`. `josi task run` requires it whenever the worker
runs: a task that is not `IMPLEMENTED` fails before the worktree is prepared
or anything is launched without one, while an `IMPLEMENTED` task is validated
and reviewed without it. OpenCode fails the run if the model is not available
in the worktree.

The escalation worker model is project configuration required for every run:

```sh
josi init --escalation-worker-model openrouter/anthropic/claude-opus-latest
```

It is stored as `escalation_worker_model` in `.josi/project.json` and defaults
to the agent's escalation default when `josi init` runs without
`--escalation-worker-model`. It is used
for the third and final worker attempt of a repair cycle. `josi task run`
requires it exactly like the reviewer model: without one the run fails before
taking the execution lock, creating a worktree, or changing the task, instead
of discovering at the last attempt that no stronger model is available. It is
never used for the first two attempts, and the reviewer model never changes
during repairs.

The reviewer model is configured independently:

```sh
josi init --reviewer-model openrouter/anthropic/claude-sonnet-latest
```

It is stored as `reviewer_model` in `.josi/project.json` and defaults to
the agent's reviewer default when `josi init` runs without
`--reviewer-model`. `josi task run`
requires it: without a reviewer model the command fails before taking the
execution lock, creating a worktree, or changing the task, instead of
silently skipping the independent review. An `IMPLEMENTED` task is validated
and reviewed without launching the worker again.

The test-writer model is project configuration required for every run:

```sh
josi init --test-writer-model openrouter/deepseek/deepseek-v4.1-flash#low
```

It is stored as `test_writer_model` in `.josi/project.json` and defaults to
the agent's test-writer default when `josi init` runs without
`--test-writer-model`. `josi task run` requires it exactly like the reviewer
and escalation models: without one the run fails before taking the execution
lock, creating a worktree, or changing the task.

The model drives the **test-generation stage**: a separate noninteractive
OpenCode turn with the built-in `build` agent that runs in the task worktree
before the first worker attempt (`opencode run --agent build --model MODEL
--session ID --format json "<prompt>"`). The turn's transcript goes into its
own timestamped log (`.josi/runs/t1-<timestamp>-tests.log`) and its usage is
recorded under the distinct `test_writer` stage. When the stage succeeds, its
decision is recorded in `.josi/testgen/t1` and never repeated - not on a
worker retry, and not after the attempt budget was deliberately reset. A
stage that was interrupted before it completed (no recorded decision and no
committed changes on the task branch) runs again on the next execution;
committed changes on the task branch from any earlier work always skip it.

The stage itself never runs the generated tests. After the worker succeeds,
the project's configured validation commands run in the task worktree, where
the test-writer commit is already part of the branch, so those commands
exercise the generated tests like any other project check. Validation
commands are project configuration and stay optional: with no commands
configured, validation is skipped and the run continues with review, and
`josi init` never adds pytest or any other validation command by default.

All four settings are written by every `josi init`: a setting that is omitted
keeps the value already stored, and a default is filled in only when no value
is stored yet. Passing an argument overrides just that setting, so a single
re-init can change one model without disturbing the others. The stored values
may also be edited directly in `.josi/project.json`.

The base branch is project-level configuration too:

```sh
josi init --base develop
```

It is stored as `base_branch` in `.josi/project.json` (the file may be edited
directly as well). Without `--base`, `josi init` uses the remote default
branch (`origin/HEAD`) when the matching local branch exists, otherwise
`main` or `master` when exactly one of them exists; when the choice is
ambiguous it lists the existing branches and asks. It never assumes the
currently checked-out branch. In a repository without commits it records the
unborn branch `HEAD` points at. A run branch is always created from the
configured base branch, task branches are created from the run branch, and a
completed run is fast-forwarded back into the base branch only by
`josi run merge`; `josi task run` refuses to start a run while that branch
has no commits - create an initial commit first, since `josi init` never
commits. Re-running `josi init` without `--base` never changes an existing
selection; a project initialized before `base_branch` existed gains one on
the next `josi init`, and `josi task run` keeps working by detecting the
branch without writing metadata.

Every stage writes its own log: `.josi/runs/t1-<timestamp>.log` for the
worker, `.josi/runs/t1-<timestamp>-tests.log` for the test-generation stage
that runs before the first worker attempt, plus
`.josi/runs/t1-<timestamp>-validation.log` and
`.josi/runs/t1-<timestamp>-review.log` when those stages run. Each log starts
with a header (task, attempt, model, branch, commit) and ends with the result
(exit codes, status, decision, verdict, failure reason), and an earlier
attempt's logs are never overwritten. The logs are written continuously while
the stage runs, and each path is printed when the stage starts as well as in
the final summary; use `tail -f` to watch a run.

### Usage and cost reporting

Every worker attempt, test-generation turn, review, and format retry of
either the test writer or the reviewer reads its usage from OpenCode's
session metadata (`GET /api/session/<id>`),
which reports the session's cumulative `cost` and token counts. For a reused
session, Josi subtracts the cumulative totals read before the turn from the
totals read after it; for a fresh session, the reported totals are that
turn's usage. Each invocation's token counts (input, output, reasoning,
cache read, and cache write) and available cost data are written to that
invocation's log and stored in `.josi/tasks.db` with its task id, run id,
stage (`worker`, `test_writer`, `test_writer_format_retry`, `review`, or
`review_format_retry`), and attempt.

The `cost_total` is OpenCode's reported cost when OpenCode provides one. When
the model's pricing metadata is also available (`GET /api/model`), Josi
derives `cost_input` and `cost_output` from the token counts and those
prices, including cache reads and writes in the input cost and reasoning
tokens in the output cost. A reported total is kept even when pricing is
missing, but then the input/output split remains unavailable. If OpenCode
reports no total and both input and output costs can be derived, the total is
their sum. If neither is available, the amount is recorded as `unavailable`,
never as zero.

`josi task show` reports totals that accumulate over every usage record for
the task: the test-generation stage, all worker attempts, reviews,
review-format retries, repairs, and separate CLI invocations. `josi run show`
reports only records attributed to
that run, so usage from the same task in an earlier or later run is not
included. The `cost_status` line summarizes the included records:

- `complete` - every included record has a total, input, and output cost.
- `incomplete` - some amounts are known but at least one is missing.
- `unavailable` - no amount is known, including when no usage was recorded.

Known amounts are still summed when the status is `incomplete`; missing
amounts stay `unavailable` instead of being presented as a confirmed zero.
Per-invocation token counts are recorded only in the logs and are not printed
by the `show` commands. For example, one invocation's log ends with:

```text
usage: input=3000 output=300 reasoning=0 cache_read=0 cache_write=0
cost: total=0.030000 input=0.009000 output=0.004500 source=reported complete=yes
usage_status: available
```

After two worker attempts and two reviews, `josi task show t1` prints the
accumulated costs:

```text
id: t1
title: Add the feature
status: STAGED
created_at: 2026-10-26T16:43:32.849+00:00
updated_at: 2026-10-26T16:43:33.317+00:00
depends_on: (none)
description:
Do the thing.
cost_total: 0.100000
cost_input: 0.018000
cost_output: 0.007200
cost_status: complete
```

`josi run show r1` prints only the usage attributed to run r1, before its
associated tasks:

```text
id: r1
status: COMPLETE
branch: josi/run/r1
base_branch: main
start_commit: 6ca6366258d1036ef8d74b664be2004a57b257ec
final_commit: 9f0ec482f951b4899d14888d9deda64eb43b4b1f
created_at: 2026-10-26T16:43:32.898+00:00
updated_at: 2026-10-26T16:43:33.323+00:00
cost_total: 0.100000
cost_input: 0.018000
cost_output: 0.007200
cost_status: complete
tasks:
t1  STAGED  Add the feature
```

If OpenCode reports a total without pricing metadata, the show commands keep
that total but mark the split incomplete:

```text
cost_total: 0.040000
cost_input: unavailable
cost_output: unavailable
cost_status: incomplete
```

Only one execution may run per project. The lock is an exclusive, nonblocking
`fcntl.flock` on `.josi/execution.lock`, and the locked file descriptor is
passed to the OpenCode worker and reviewer (Python's `pass_fds`), so the lock
stays held while those processes are alive even if Josi itself is killed. A
multi-task run holds that one lock for the whole batch - it is never released
between tasks - so a concurrent `josi task run` recognizes the active
execution immediately; `josi run merge` takes the same lock while it
integrates a run. A run that is left `RUNNING` by an interrupted execution is
resumed by the next `josi task run`: a project never has two running runs.
Planner operations (`josi plan`, `josi task create`, ...) stay separate from
execution and never take the lock.

## Repair and escalation

A validation command that exits non-zero and a reviewer verdict of
`changes_requested` (which always carries at least one critical or major
finding) are implementation failures: Josi feeds them back to the next
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

[14:00] Preparing worktree
[14:00] Created worktree .josi/worktrees/t1 (branch josi/t1)
[14:00] Test writer started (model tests/model)
[14:00] tests log: .josi/runs/t1-20260101T140000123456Z-tests.log
[14:01] Test writer finished with exit code 0
[14:01] Test generation finished (no tests needed)
[14:01] Worker attempt 1/3 (provider/model)
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
[14:13] Fast-forward merging into run r1...

Task t1 → STAGED (run r1)

Summary: completed: t1
[14:13] Run r1 finished with 1 staged task; integrate it with 'josi run merge r1'
```

Approval is immediately followed by staging the approved commit into the run
branch (see "Staging into the run branch" above); the
`Task t1 → APPROVED (not staged)` line appears instead when the merge was
refused, and the task then keeps its approved state for a later retry. A
`--all` or multi-id run wraps the same events and ends with a summary such as
`Summary: completed: t1, t2`. The run is not integrated automatically: run
`josi run merge r1` to move its staged tasks to `DONE`.

Attempt accounting lives in `.josi/attempts/t1`, a small JSON record with the
number of used attempts and the feedback for the next worker, so restarting
`josi` never silently resets the budget. When all three attempts are used the
task ends `NEEDS_HUMAN`; re-running it is refused until a human intervenes.
Deleting `.josi/attempts/t1` is the explicit intervention that grants a fresh
three-attempt budget, and the preserved branch and worktree are reused.
`josi task reset t1` is the stronger, command-line form of that intervention:
it also returns the task to `PENDING` and discards its branch, worktree, stored
reviews, and recorded test-generation outcome, so the task starts over from
scratch - including a fresh test-generation stage - instead of continuing on
the existing commits.

## Validation and review

Validation commands are project configuration in `.josi/project.json`:

```json
{
  "validation": {
    "commands": ["pytest", "ruff check ."]
  }
}
```

The list is entirely the project's choice: `josi init` never writes a
`validation` section and never adds pytest or any other validation command by
default. The tests the test-generation stage commits are exercised by these
commands after the worker, not by the stage itself.

- Commands run sequentially in the task worktree, exactly as written (a
  shell), and stop at the first non-zero exit.
- Output, exit codes, and the overall result are recorded in the validation
  log. A failed command with attempts left triggers a repair attempt with the
  command and its output as feedback; with no attempts left the task ends
  `NEEDS_HUMAN`, and no reviewer starts for that attempt.
- After every successful command, Josi immediately re-checks the recorded
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
  validated commit; if it moved, the run stops and the task fails. Josi never
  repairs or resets the branch.

The commit a task branch was created from is recorded in `.josi/base/t1` when
the branch is created, so the review diff always starts at the original base
even if the project's base branch is reconfigured later. Branches created
before this record existed (Stage 4 projects) fall back to the merge base
with the configured base branch.

After validation passes, the reviewer runs in a temporary detached worktree
under `.josi/review/t1-<timestamp>` created at the validated commit, never in
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
tolerated, but prose, several objects, and truncated JSON are refused. When
a nonempty final message is not one JSON object, Josi asks the same reviewer
once to reformat its existing verdict, preserving the decision and every
finding.

This formatting-only correction uses the same session and isolated checkout,
does not repeat validation or consume a worker attempt, and is recorded in
the same review log. Only the correction turn's output is parsed, with the
same strict validation and checkout/commit integrity checks.

Output that remains malformed, unknown decisions or severities, an approval
that carries a critical or major finding, and a `changes_requested` verdict
without a blocking finding are all invalid: the task becomes `FAILED` and
nothing is recorded as an approval. Every critical or major finding must
include a scenario, impact, and location. Valid verdicts are stored in
`.josi/tasks.db` (the `reviews` table) with the commit they refer to, and
Stage 6 feeds the critical and major findings back to the next worker
attempt. Reviewer execution failures, invalid verdicts, and format failures
that persist after the one correction are recorded in the review log with a
reason; they fail the task and never trigger a repair worker. Semantic
errors (such as an approval with a blocking finding) are not retried or
automatically repaired.

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
staging described above: the task becomes `STAGED` once its run branch has
fast-forwarded to that commit, keeps `APPROVED` when the staging was refused,
and only becomes `DONE` when the run is integrated with `josi run merge`.
Nothing is ever pushed to a remote.

## Planner agent

On the first `josi plan` (or whenever the file is missing), Josi installs

```
.opencode/agents/josi-planner.md
```

an OpenCode custom primary agent. Its body is the planner's **system prompt**:
discuss requirements and design solutions, decompose goals into tasks with
descriptions and acceptance criteria, manage the task list only through
`josi task`, never modify code, and never execute tasks. Its permissions are
deny-by-default: a catch-all rule blocks every tool, and only the native
read-only inspection tools (`read`, `glob`, `grep`), questions, and the
`josi task` management subcommands are re-allowed. Editing, subagents, web
access, and MCP tools stay denied - including tools that write files under an
action name other than `edit` - and reading a `.env` file still asks.
`josi task run` and `josi run` are denied outright, so the planner can manage
the task list but can never launch a worker or integrate a run into the base
branch.

The `josi task create`, `josi task list`, `josi task show`,
`josi task update`, and `josi task delete` subcommands are the only
pre-approved shell commands: the planner can use them without a permission
prompt, while every other shell command still requires explicit user approval.
A broad `josi task *` deny comes before those management allows (OpenCode
evaluates permission rules last-match-wins), so only the documented forms are
pre-approved and `josi task run` - including whitespace variants such as
`josi task  run` - stays denied; `josi task reset` stays denied too (it
discards a task's budget, branch, and worktree), and `josi run *` is denied as
well, so the planner can never integrate a run. The denials were verified
against the installed OpenCode 2.1.2 permission engine.
The prompt tells the planner to prefer the native `read`, `glob`, and `grep`
tools over shell `cat`, `head`, `tail`, `ls`, or `grep` (the `read` tool
pages through files with offset/limit), and to run inspections one per shell
call (each command in a compound `&&` call is checked separately, and helpers
like `echo` still require confirmation). Because the instructions are
system-level, launching the planner does not start a conversation by itself -
the TUI just waits for input.

Shell **redirection** (`>`, `>>`, `2>&1`) is denied outright. OpenCode
reports a redirected statement as a single resource string, so a management
allow rule like `josi task list *` would also match
`josi task list > src/file` and could silently overwrite source files; the
explicit deny rule comes after the management allows (OpenCode evaluates
permission rules last-match-wins), which closes that hole. As a consequence,
a literal `>` inside a quoted task title or description counts as redirection
and is denied too; reword the text or run that command yourself outside the
planner.

Josi deliberately pre-approves no other shell commands: OpenCode matches a
broad allow rule like `git diff *` or `cat *` against commands that write
files as well (`git diff --output=file`, `cat file > file`), which would
silently defeat the no-modification rule. Approve individual inspections in
the permission dialog; choose *Allow always* there if you want a specific
command pre-approved - OpenCode saves a project-scoped rule that Josi does
not override (it is evaluated after the agent rules but cannot override an
agent deny).

The rule format is verified against the installed OpenCode 2.1.2: a
permission rule is `{action, resource, effect}` with effect `allow`, `deny`,
or `ask`; action and resource are matched as glob patterns (`*`, `?`) and
the last matching rule wins, so ordering matters. Shell commands are checked
one resource per command, with `action: shell` and the parsed command text
as the resource.

Josi binds every new planner session to this agent. (The Claude Code
backend does not use this file.)

The agent file is **managed by Josi**: if its content differs from the
definition shipped with the installed Josi version (for example after an
upgrade changes the permissions or the prompt), the next `josi plan` rewrites
it and prints an `Updated the planner agent definition` notice. Do not hand-edit
that file; put project-specific guidance in your own `AGENTS.md` instead.

## How it works

Josi delegates all session management to OpenCode - it does not store
conversations itself. It uses these OpenCode surfaces:

| Purpose | Command |
| --- | --- |
| Create a session at the project root | `opencode api POST /api/session` (explicit `location.directory` and `agent`) |
| Verify a stored session belongs to the project | `opencode api GET /api/session/<id>` |
| Re-select the planner agent on an old session | `opencode api POST /api/session/<id>/agent` |
| Open the planner TUI | `opencode <project-root> --session <id>` |
| Create a worker session in a task worktree | `opencode api POST /api/session` (location and agent `build`) |
| Run the worker noninteractively | `opencode run --agent build --model <model> --session <id> "<prompt>"` |
| Create a test-writer session in a task worktree | `opencode api POST /api/session` (location and agent `build`) |
| Run the test writer and read its decision | `opencode run --agent build --model <model> --session <id> --format json "<prompt>"` |
| Create a reviewer session in a temporary worktree | `opencode api POST /api/session` (location and agent `plan`) |
| Run the reviewer and get a machine-readable verdict | `opencode run --agent plan --model <model> --session <id> --format json "<prompt>"` |

Sessions are created explicitly (instead of letting the TUI create them
lazily) so Josi captures the real session ID. Before resuming, Josi asks
OpenCode for the session and compares its `location.directory` with the
project root; if the session is missing, unreadable, or belongs elsewhere,
Josi reports an error instead of resuming the wrong session. These processes
inherit the normal process environment, including any OpenCode configuration
variables such as `OPENCODE_CONFIG_CONTENT`, which Josi never sets or alters.

### OpenCode configuration and provider routing

Josi does not configure OpenCode or choose a provider route: it only passes the
model names stored in `.josi/project.json` (for example
`openrouter/anthropic/claude-sonnet-latest`) and leaves everything else to
OpenCode. Provider-routing preferences, such as OpenRouter provider ordering or
price sorting, are therefore not a Josi default. To set one, use OpenCode's own
configuration (`opencode.json`, the `OPENCODE_CONFIG` file, or the
`OPENCODE_CONFIG_CONTENT` variable) as described in the OpenCode documentation;
Josi-launched processes pick it up like any other OpenCode run.

## Project files

```
.josi/
├── project.json    # schema_version, name, agent, worker_model, reviewer_model,
│                   # escalation_worker_model, test_writer_model,
│                   # validation.commands, base_branch, created_at
├── tasks.db        # SQLite database (tasks, dependencies, reviews,
│                   # runs, run selections, per-invocation usage)
├── execution.lock  # project-wide execution lock (fcntl.flock)
├── base/           # branch-creation commit per task: t1, t2, ...
├── attempts/       # used worker attempts + next-attempt feedback: t1, t2, ...
├── testgen/        # recorded test-generation outcome per task: t1, t2, ...
├── runs/           # tN-<timestamp>.log worker, -tests.log, -validation.log,
│                   # -review.log
├── review/         # temporary reviewer worktrees (removed after review)
├── integrate/      # temporary merge worktrees (removed after the merge)
├── run-worktrees/  # one Git worktree per run: r1/, r2/, ...
└── worktrees/      # one Git worktree per task: t1/, t2/, ...

.opencode/
└── agents/
    └── josi-planner.md   # planner agent: system prompt + task permissions
```

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests use a fake `opencode` executable on `PATH`, so they need no network
access, no real OpenCode process, and no model calls. The planner tests
evaluate the installed agent's permission rules with a matcher that mirrors
OpenCode 2.1.2 (last matching rule wins, `*`/`?` globs, `ask` by default) and
run the pre-approved `josi task` commands against a real project database.
The Stage 4 tests drive `josi task run` against disposable Git repositories;
the fake worker can commit, leave a dirty worktree, fail, or keep running
while the test kills Josi, so branch and worktree handling, retries, and the
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
requiring a worker model. The test-generation suite (`test_testgen`) covers
the stage that runs before the first worker attempt: the test writer's
session, configured model, JSON output format, task worktree, and position
before the worker; `no_tests_needed` succeeding without a commit and
`tests_written` counting only together with committed test changes; invalid
outcomes, execution errors, uncommitted or untracked changes, and empty
commits failing the stage before the worker starts; a prose-wrapped or
otherwise malformed final message being reformatted once without losing the
decision or the commit, a bounded retry, and an unusable decision or a
branch-changing retry failing the stage; the stage's own
`-tests.log` and the separate `test_writer` usage records; the worker prompt
keeping the generated tests unchanged; the completed stage persisting across
retries, repair attempts, and resumes while a failed stage reruns without
spending a worker attempt; and the test-writer model's `josi init` defaults,
overrides, malformed values, and missing-model preflight before the execution
lock. The generated tests are exercised by the project's own validation
commands after the worker, and validation stays optional: `josi init` adds no
validation command by default. The Stage 6 tests cover the repair loop: approval on
the first attempt, minor findings without a retry, a second attempt with
reviewer feedback, escalation on the third attempt, `NEEDS_HUMAN` after three
rejections or three validation failures, validation feedback, cumulative
review diffs, preserved worktrees and commits, separate per-attempt logs,
reviewer failures that never trigger a repair and never spend a worker
attempt on an explicit retry, the persisted attempt budget across invocations
(including an interrupted run and legacy `NEEDS_CHANGES` tasks), the explicit
reset path, the required escalation model and its preflight ordering before
the execution lock, and the escalation model configuration. The Stage 7
tests cover run-level integration and sequential execution: the run branch
and worktree being created from the configured base branch; several tasks
staged sequentially into one run branch, each starting from the previous
staged task's result; the base branch staying untouched during execution;
STAGED dependencies satisfied inside the same run and blocking from another
run (including the exact actionable message); fast-forward integration and
the `STAGED -> DONE` transitions; detection of a manual merge, plain or with
a merge commit; a repeated `josi run merge` being safe; a non-fast-forward
that requires manual Git integration and then finalizes the statuses; a
manual merge detected after the run branch was cleaned up; a dirty base
worktree and a run branch that moved after completion; a failed batch
preserving its staged work, worktrees, logs, and open run and continuing in
the same run when resumed; a commit made by hand in the run worktree being
refused and never recorded or integrated; an ignored local file the merge
must never overwrite; branch squash options that cannot fake a successful
integration; already approved, staged, and DONE tasks; explicit task lists;
`--all` in dependency order; deterministic id order for independent tasks;
stopping at the first failure or at a task that needs a human;
`josi run list` and `josi run show`; and one execution lock per batch. A
dedicated launch-environment suite (`test_launch_config`) checks that no
launch path - the planner TUI, the worker, and the reviewer, including the
worktree launches - receives Josi-generated OpenCode configuration, and that
an environment supplied by the caller reaches OpenCode unchanged.

## Limitations

- Targets OpenCode v2; the `opencode api` surface is still marked experimental
  by OpenCode. If a future OpenCode release changes it, session creation and
  verification will fail with a clear error rather than do the wrong thing.
- Every `josi plan` starts a fresh planner session; earlier sessions remain
  available inside the agent CLI.
- Josi must be run from inside the project. It locates the Git repository
  root (even from a subdirectory) and requires `.josi/` there, so a nested
  Git repository is never treated as part of its parent's Josi project.
- Josi writes its managed files (`.josi/`, `.gitignore`, and the planner
  agent) with plain file writes that follow symlinks. A repository containing
  a hostile symlink at one of those paths can redirect the write. Run Josi
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
  requests or GitHub/GitLab integration, no automatic crash recovery, and no
  parallel or background runs: only one run executes at a time, an
  interrupted or stopped run is resumed by the next `josi task run`, and a
  completed run stays unintegrated until the user runs `josi run merge`.
- `IMPLEMENTED` only means that the worker exited successfully, left the
  worktree clean, and left committed changes on the task branch relative to
  the run branch. `APPROVED` additionally means the configured validation
  commands passed on that exact commit and the reviewer approved it, possibly
  after repairs; `STAGED` then means that exact commit is part of the run
  branch, and `DONE` means the whole run was fast-forwarded into the base
  branch. None of these states is a guarantee of correctness.
- `josi task run` requires a reviewer model and an escalation worker model
  for every run, and a worker model whenever the worker runs. Without any of
  them the run fails before launching anything; an `IMPLEMENTED` task is
  validated and reviewed with just the reviewer and escalation models, and
  an `APPROVED` task is staged the same way, without rerunning the worker.
- A validation command that changes tracked files or moves `HEAD` fails the
  task immediately and is never repaired by another worker: the cause is the
  validation command, not the implementation. Josi reports it instead of
  burning attempts on it.
- A corrupt `.josi/attempts/tN` record fails the task instead of silently
  resetting the repair budget; fix or remove the record deliberately.
- Validation commands run with Josi's environment inside the task worktree
  and may create untracked files (caches, reports). Changes to tracked files
  or to `HEAD` fail the task as soon as a command finishes; Josi does not
  clean anything up.
- Josi never resets, cleans, rebases, or deletes a task worktree or a run
  worktree. A task that fails keeps its branch, worktree, and log for an
  explicit retry, and a failed batch keeps its run branch and every staged
  commit. Two kinds of worktree are temporary and removed again: the
  reviewer's checkout and the merge worktree used when no worktree has the
  integration target checked out.
- Every automatic Git integration is a plain `git merge --ff-only` of a
  validated commit: staging an approved task into the run branch, and
  integrating a completed run into the base branch. `--no-squash` and
  `--no-overwrite-ignore` keep branch configuration from staging a squash
  and from replacing an ignored local file. Josi never rewrites the base
  branch: when it advanced with its own commits, `josi run merge` is refused
  and a human must reconcile the run by hand (then the same command detects
  the manual merge and finalizes the statuses). Merging also refuses while an
  uncommitted or untracked file sits in the worktree that has the base branch
  checked out, instead of touching the user's work. When no worktree has the
  base branch checked out, a temporary worktree performs the merge, so that
  checkout does not see the new files until it is updated.
- `josi task run --all` does not select `STAGED`, `DONE`, or `NEEDS_HUMAN`
  tasks. It reports the tasks that still need a human and stops when no
  selected task can progress, so a project with an abandoned `NEEDS_HUMAN`
  task can still finish and report success for everything else that was
  runnable. A task whose dependency is staged in another run is refused with
  the exact `josi run merge` command; Josi never integrates that other run
  automatically.
- The execution lock covers Josi and the OpenCode processes it spawns. A
  detached descendant that does not keep the inherited descriptor (for example
  a daemonized helper the worker starts) can outlive the lock; Josi keeps no
  PID tracking, heartbeats, or automatic recovery.
- The worker runs with OpenCode's standard `build` agent, so it may modify
  anything inside its worktree. A noninteractive run cannot ask for
  permissions: a request that would prompt in the TUI (for example reading a
  `.env` file or touching a path outside the worktree) is auto-rejected and
  the worker continues without it. Josi does not pass `--auto`, so those
  guards stay active. Like the planner, this is not a sandbox - an approved
  task is staged into its run branch automatically, so review the run
  branch (and later the merge) instead of trusting the worker.
- The worker prompt asks the worker to commit everything and never merge
  into the run branch, and Josi checks afterwards that the worktree is still
  on its task branch, is clean, and contains committed changes relative to
  the run branch; a worker that ignores the instructions is reported as a
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
  commit into the run branch, review what the worker produced with
  `git log`/`git diff` on the run branch; the base branch only moves when
  the user runs `josi run merge`. Josi never pushes, so a wrong merge can
  still be corrected locally.
- A stopped or interrupted run stays `RUNNING` and is resumed by the next
  `josi task run`, so a failed batch continues in the same run and never
  opens a second one. A run is `COMPLETE` only when every selected task is
  staged (or already DONE); only then can `josi run merge` integrate it.
  Because every new run starts from the current base branch, integrate a
  finished run before starting work that builds on it.
- The run branch tip is verified against the run's recorded expected commit
  before resumption, staging, and completion, so a commit made by hand in
  the run worktree is refused and can never be recorded as the final commit
  or merged into the base branch. Josi never resets the branch to recover;
  the user restores or removes it explicitly. Runs created before the
  expected commit existed (no recorded value) are not verified.
