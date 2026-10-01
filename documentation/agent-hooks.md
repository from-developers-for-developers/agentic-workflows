# Agent hooks

ww's instructions to agents are static, so they cannot say which task is
half done. Agent hooks are the agent's own hooks, installed with `ww hook`
— not the workflow hooks of `ww.yaml` (see
[Workflow hooks, variables, and transitions](features.md#workflow-hooks-variables-and-transitions)
in the feature reference), which run as part of a plan and can run
handlers, gate completion, or trigger a check. Agent hooks carry ww's task
state into a session instead: they are gentle by design, adding a few
lines of context or reminding once, and nothing they do ever blocks the
agent.

| Hook | When it runs | What it does |
| --- | --- | --- |
| `session-start` | A session starts, resumes, or is compacted | Prints a reminder that ww coordinates work here (under `"enabled": "on_request"`: that ww is used only when the user asks for it) and up to five unfinished tasks written within `agent_hooks.recent_days`, newest first, with their step, workspace, and resume commands. |
| `stop` | The agent ends its turn | Once per step attempt, asks the agent to record an agent-owned step it left in progress. The next stop is always allowed. |
| `interrupt` | The session ends or is interrupted mid-step | Records the interruption without answering, so the next session is told to check the work. When the step was talking with the operator, it first recovers the conversation from the session's transcript (Claude Code and Codex), and the notice points at it. |

There is no pre-spawn hook yet: ww does not intercept an agent's own
subagent calls. `subagents: false` is enforced only through the step's
page today; see [Agent limitations](limitations.md#agent-limitations).

## What `session-start` prints

What `session-start` prints stays in the agent's context for the whole
session, so it is short:

```text
This project coordinates work through ww: `./ww discover` lists its workflows.
Unfinished ww tasks, newest first:
- TASK-16 (task, claudecode) develop: in progress · in ww-worktrees/TASK-16 · resume: `./ww instruction TASK-16 --role manager` · worker: `./ww instruction TASK-16 --run 01-task --role worker`
  Left in progress at 2026-09-28T18:40:00Z by claudecode with no recorded end, probably a closed session: check its page (`./ww instruction TASK-16 --role manager`) before continuing.
`./ww discover` lists every unfinished task.
```

The scan reads only the tasks whose state was written within the last
`agent_hooks.recent_days` days (3 by default), judged by the state file's
modification time before anything is parsed, so a long task history costs
nothing; `discover`, `lookup` and `interrupted` still look at every task.
Whenever a task was listed or skipped, a last line points at `discover`.

A tab closed abruptly, a crash or a kill never runs the `interrupt` hook, so
the task's step is still in progress with no [interruption
marker](#interrupted-tasks). Such a task gets the "Left in progress … with no
recorded end" line above; a step talking with the operator is told instead
that its page shows the conversation so far, to pick up at the last
unanswered question. A marked task gets only its interruption notice, and
after a compaction the line is left out, since the session holding the step
is the one carrying on.

`ww.json` tunes the scan:

```json
"agent_hooks": {"check_unfinished": true, "recent_days": 3}
```

`check_unfinished: false` switches the scan off: `session-start` prints only
its one-line reminder, while `stop` and `interrupt` work as before.
`recent_days` is also the window of `ww interrupted` and of the `discover`
and `lookup` pointer below.

A task whose state cannot be read never silences the hooks. `session-start`
lists the readable tasks as usual and adds one line naming the others:

```text
ww cannot read the state of TASK-20; other tasks and new work are unaffected. `./ww discover` shows why; ask the operator before touching them.
```

`stop` and `interrupt` consider only the tasks that could be read, and
`interrupted` and `lookup` skip a marked task they cannot read.

After a compaction it starts with "Context was compacted; ww's task state is
authoritative." Every recent unfinished task is listed, whichever agent started it,
with that agent after the workflow, since other sessions' open work is useful
context. A task waiting for the operator stays listed and says so, for
example `awaiting the operator: the work failed`. Work attached to a step as a
workflow hook is named by itself and its step, for example
`update-documentation (a hook of run-tests)`, never by the step alone. Only the main session gets
this context: no hook is registered for a subagent's start, since a worker
receives its bootstrap command from the manager.

## `stop` and `interrupt`

`stop` answers "continue" only while an agent-owned step is dispatched and in
progress, not waiting for input or the operator, and only the first time for
that step attempt. Its message names the step and says to run `complete` when
the work is done or `fail` when it cannot finish, and that a manager waiting on
a worker, or a deliberate pause, may simply stop again. Agents that report their
own loop guard (Claude Code's and Codex's `stop_hook_active`, Cursor's
`loop_count`) are always allowed once they have continued.

Which tasks concern a session:

- A session inside a task's worktree or project directory concerns that task,
  whichever agent started it; the most specific workspace wins.
- The root is not a task's workspace, even for a task without a worktree. A
  session in the root, anywhere else, or that names no directory concerns only
  the tasks its own agent started, so a Claude Code session is never reminded
  about a Codex session's step.
- A manager waiting on a worker is not reminded. On a run started with
  `--runtime auto`, a step that may go to a worker (`role: worker`, the
  default) is delegated: the main session's stop skips it, and the worker's
  own stop (`SubagentStop` in Claude Code and Codex, `subagentStop` in Cursor)
  reminds instead. A step the manager performs itself (`role: manager`) is
  still reminded. Antigravity reports no worker stop, so its manager is
  simply not reminded about delegated steps.
- An interactive step whose conversation has not ended is not reminded: the
  session stops so the operator can answer. Once `interact --end` was
  recorded, the step is open work again and the next stop reminds. An
  `interrupt` during such a conversation is still recorded, and the notice
  points at the conversation on the step's page instead of at `git status`.

The agent records an interactive step's conversation once, when it ends, so a
session that ends mid-conversation would lose it. The `interrupt` hook is the
safety net: Claude Code and Codex name the session's transcript file in every
hook payload as `transcript_path`, and for a task whose step is still talking
with the operator ww reads the last 2 MB of that file. It keeps the operator's
typed messages and the agent's text replies newer than the start of the step's
attempt, skipping tool calls and results, reasoning, injected context and
subagent lines, and appends them to the task's `interactions.md` under the
speakers `operator (recovered)` and `agent (recovered)`. Claude Code writes the
file asynchronously, so a `last_assistant_message` in the payload fills in the
agent's last reply. The interruption marker keeps the count, and the notice says
"N entries of the conversation were recovered from the session transcript", or
that the conversation was not recorded. Only the extracted entries are stored,
never the transcript. Reading it is best effort: an unknown line is skipped, and
any failure recovers nothing rather than failing the hook. Cursor and
Antigravity have no transcript reader, so their conversations are not recovered.

## Agents and their files

| Agent | Project file | Native events | Local file |
| --- | --- | --- | --- |
| Claude Code | `.claude/settings.json` | `SessionStart`, `Stop`, `SubagentStop`, `SessionEnd` | `.claude/settings.local.json` |
| Codex | `.codex/hooks.json` | `SessionStart`, `Stop`, `SubagentStop`, `Interrupt`, `SessionEnd` | none; user file `~/.codex/hooks.json` |
| Cursor | `.cursor/hooks.json` | `sessionStart`, `stop`, `subagentStop`, `sessionEnd` | none; user file `~/.cursor/hooks.json` |
| Antigravity | `.agents/hooks.json`, group `ww` | `PreInvocation`, `Stop` | none; user file `~/.gemini/config/hooks.json` |

Claude Code's `SessionStart` has no matcher, so it fires for every source,
compaction included. Pressing Esc fires no hook in Claude Code; the end of the
session does. Cursor reports an aborted turn as a `stop` with status
`aborted`, which ww treats as an interruption rather than a reminder.
Antigravity has no session-start event, so ww answers only its first
pre-invocation of a conversation, as an ephemeral message that never
accumulates; it names an interruption through the Stop hook's
`terminationReason`, which ww reads on a best-effort basis. Codex loads project
hooks only once the project's `.codex/` layer is trusted: review them with
`/hooks` in Codex.

These per-agent gaps (no worker-stop event, no session-start event, a trust
gate) are permanent differences between agents, not bugs; see
[Agent limitations](limitations.md#agent-limitations) for the summary.

## Install, remove, and show

```console
ww-agentic-workflows hook install --agent claudecode
ww-agentic-workflows hook uninstall --agent codex
ww-agentic-workflows hook show --agent cursor
ww-agentic-workflows hook install --agent claudecode --local
```

`install` merges ww's entries into whatever the file already holds and leaves
every other entry alone. ww recognises its own entries by their command, so a
second `install` changes nothing and `uninstall` removes exactly what ww added.
`show` prints the file and the exact snippet, for a machine or an agent where
`install` cannot run. A file that is not valid JSON, or cannot be written,
makes `install` fail with the path and the full snippet to add by hand.

`--local` writes the agent's project file kept out of version control, so the
hooks stay one person's choice. Only Claude Code has one,
`.claude/settings.local.json`; for the other agents `--local` fails and names
their user-level file, which applies to every project on the machine. Without
`--local` the hooks go into the shared project file, which is usually committed,
so everyone who clones the project gets them. When ww's hooks sit in both of
Claude Code's files every hook would run twice, so `install` and `show` print a
notice with the command that removes one copy.

The registered commands call the project's tracked `./ww` launcher: through
`"$CLAUDE_PROJECT_DIR"` for Claude Code, from the project root for Cursor, and
from the repository's top level (`git rev-parse --show-toplevel`) for Codex and
Antigravity. The launcher resolves the durable root through the shared Git
directory, so a session in a task worktree reaches the same state; the launcher
must therefore be tracked for worktree sessions.

The runtime form, `ww hook session-start|stop|interrupt --agent <agent>`, is
what the agents call; it reads the agent's JSON payload on stdin.

## When a hook fails or runs too often

A hook never breaks the agent. On any error, a bad argument, or outside a ww
root, it exits 0 with no output: some agents read exit code 2 as "continue", and
an unexpected message would enter the agent's context. The worst outcome is a
lost hint. When the command cannot start at all, for example a missing
launcher or an untrusted Codex hook, the agent shows its own non-blocking hook
error. A hook that times out is killed by the agent, which carries on.

Running more often than needed is harmless. The stop reminder is claimed under
a lock of its own, so parallel calls, such as Claude Code running matching
hooks at once, remind exactly once. A repeated interrupt overwrites the marker,
recorded in `stop-reminders.json` and `interrupted.json` beside each task's
other records (see [Interrupted tasks](#interrupted-tasks) below).
Antigravity calls its pre-invocation hook before every model call; only the
first one returns context.

Every hook call is written to the audit log, `.ww/executions.jsonl`, with the
event, the agent, and ww's decision, so a session can be followed there:

```json
{"command": "hook", "hook_event": "stop", "hook_agent": "claudecode", "hook_decision": "reminded: TASK-16", "outcome": "ok"}
```

Decisions read like `context with 2 unfinished task(s)`, `allowed`,
`reminded: TASK-16`, or `marked interrupted: TASK-16`; a failure is logged with
`outcome: error` and the exception type. The agent's payload is never logged.

## Interrupted tasks

When a session ends or is interrupted while an agent-owned step is in progress,
ww writes `.ww/tasks/<task-id>/interrupted.json` with the time, run, step,
attempt, agent, and the reason the agent gave. The next interruption
overwrites it. ww marks the tasks that concern the session, chosen as for the
stop reminder: the task whose worktree or project directory the session worked
in, otherwise the tasks with a step in progress that the same agent started. ww
never commits on an interruption: the tree is often half applied at that
moment, and commits belong to the workflow.

While the marker applies, `status`, `instruction`, and `next` lead with a
notice, and `session-start` adds it under the task's line:

```markdown
> Interrupted: the previous session (claudecode, prompt_input_exit) stopped at 2026-09-28T18:40:00Z during `develop` (attempt 1). Before continuing, check `git status` in the task workspace, review the diff and ww's recorded commits (`./ww extension ww/git commits TASK-16`), and compare them with the step's requirements; then complete, continue, or fail the step.
```

The marker clears once that step attempt completes or fails, or is replaced by
a later attempt, and when the task is reset. It is never cleared merely by being
shown, so a compaction or a new session between reading it and acting keeps
it. A run that is abandoned keeps its marker.

`ww interrupted` lists the marked tasks, newest first, from the markers
themselves; there is no second index to drift:

```console
ww-agentic-workflows interrupted
ww-agentic-workflows interrupted --since 7
ww-agentic-workflows interrupted --all --json
```

`--since` defaults to `agent_hooks.recent_days` (3). `discover` and `lookup`,
the commands an agent without hooks runs first, add one line only when that
window holds an interruption, and `interrupted_recently` in their JSON:

```text
1 task was interrupted in the last 3 days; run `./ww interrupted` before starting new work.
```

`discover` also lists every unfinished task, however old, under "Unfinished
tasks", one line each as `session-start` prints it, with the interruption
notice of a marked task.

## See also

- [Agent limitations](limitations.md#agent-limitations): per-agent hook
  gaps, nested subagents, and why a worker cannot talk to the operator.
- [architecture.md](architecture.md#agent-hooks) for how the hook answers
  are computed internally.
