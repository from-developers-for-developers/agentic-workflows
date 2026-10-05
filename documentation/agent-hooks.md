# Agent hooks

ww's instructions to agents are static, so a session needs one pointer to
find ww at all. Agent hooks are the agent's own hooks, installed with `ww hook`
— not the workflow hooks of `ww.yaml` (see
[Workflow hooks, variables, and transitions](features.md#workflow-hooks-variables-and-transitions)
in the feature reference), which run as part of a plan and can run
handlers, gate completion, or trigger a check. Agent hooks are gentle by
design: they add one line of context, and nothing they do ever blocks the
agent.

ww registers one hook, `session-start`, which runs when a session starts,
resumes, or is compacted. It prints a reminder that ww coordinates work here
(under `"enabled": "on_request"`: that ww is used only when the user asks for
it) and how to list its workflows. It lists no task: `discover` lists the
project's workflows, and the operator names the task to continue.

There is no pre-spawn hook yet: ww does not intercept an agent's own
subagent calls. `subagents: false` is enforced only through the step's
page today; see [Agent limitations](limitations.md#agent-limitations).

## What `session-start` prints

What `session-start` prints stays in the agent's context for the whole
session, so it is one line:

```text
This project coordinates work through ww: `./ww discover` lists its workflows.
```

After a compaction it starts with a line before that one: "Context was
compacted; ww's task state is authoritative." Only the main session gets this
context: no hook is registered for a subagent's start, since a worker receives
its bootstrap command from the manager.

## `stop` and `interrupt` are retired

Earlier versions of ww also registered `stop` and `interrupt` hooks, which
listed unfinished tasks, reminded an agent to close a step, and recorded
interrupted sessions. They are gone: a reminder written for one session
confused another, and nothing in them was needed to continue a task.

The events stay accepted so a hooks file written by an older ww keeps
working. `ww hook stop` and `ww hook interrupt` exit 0 with no output and
record nothing; the audit log notes the call. The `agent_hooks` keys
`check_unfinished` and `recent_days` in `ww.json` are still validated and
still accepted, and have no effect. Running `hook install` again replaces an
older registration with the one `session-start` entry, and `hook uninstall`
removes every ww entry, whichever events it registered.

## Agents and their files

| Agent | Project file | Native event | Local file |
| --- | --- | --- | --- |
| Claude Code | `.claude/settings.json` | `SessionStart` | `.claude/settings.local.json` |
| Codex | `.codex/hooks.json` | `SessionStart` | none; user file `~/.codex/hooks.json` |
| Cursor | `.cursor/hooks.json` | `sessionStart` | none; user file `~/.cursor/hooks.json` |
| Antigravity | `.agents/hooks.json`, group `ww` | `PreInvocation` | none; user file `~/.gemini/config/hooks.json` |

Claude Code's `SessionStart` has no matcher, so it fires for every source,
compaction included. Antigravity has no session-start event, so ww answers
only its first pre-invocation of a conversation, as an ephemeral message that
never accumulates. Codex loads project hooks only once the project's `.codex/`
layer is trusted: review them with `/hooks` in Codex.

These per-agent gaps (no session-start event, a trust gate) are permanent
differences between agents, not bugs; see
[Agent limitations](limitations.md#agent-limitations) for the summary.

## Install, remove, and show

```console
ww-agentic-workflows hook install --agent claudecode
ww-agentic-workflows hook uninstall --agent codex
ww-agentic-workflows hook show --agent cursor
ww-agentic-workflows hook install --agent claudecode --local
```

`install` merges ww's entry into whatever the file already holds and leaves
every other entry alone. ww recognises its own entries by their command, so a
second `install` changes nothing and `uninstall` removes exactly what ww added,
including the `stop` and `interrupt` entries an older ww registered. `show`
prints the file and the exact snippet, that is, what ww registers, for a
machine or an agent where `install` cannot run. A file that is not valid JSON,
or cannot be written, makes `install` fail with the path and the full snippet
to add by hand.

`--local` writes the agent's project file kept out of version control, so the
hooks stay one person's choice. Only Claude Code has one,
`.claude/settings.local.json`; for the other agents `--local` fails and names
their user-level file, which applies to every project on the machine. Without
`--local` the hooks go into the shared project file, which is usually committed,
so everyone who clones the project gets them. When ww's hooks sit in both of
Claude Code's files every hook would run twice, so `install` and `show` print a
notice with the command that removes one copy.

The registered command calls the project's tracked `./ww` launcher: through
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

Running more often than needed is harmless. Antigravity calls its
pre-invocation hook before every model call; only the first one returns
context.

Every hook call is written to the audit log, `.ww/executions.jsonl`, with the
event, the agent, and ww's decision, so a session can be followed there:

```json
{"command": "hook", "hook_event": "session-start", "hook_agent": "claudecode", "hook_decision": "context", "outcome": "ok"}
```

A failure is logged with `outcome: error` and the exception type. The agent's
payload is never logged.

## See also

- [Agent limitations](limitations.md#agent-limitations): per-agent hook
  gaps, nested subagents, and why a worker cannot talk to the operator.
- [architecture.md](architecture.md#agent-hooks) for how the hook answers
  are computed internally.
