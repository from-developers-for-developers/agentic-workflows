# Task: enforce `subagents: false` with agent hooks

Status: planned, 2026-09-30. Depends on TASK-22 (assignment tokens and the
`subagents` meaning), merged to `dev`.

## Goal

Today `subagents: false` is only an instruction on the step page. This task
makes it a guarantee: while a task has a `subagents: false` step in progress,
ww's agent hook refuses every subagent spawn except ww's own dispatch of the
open assignment.

## Rule the hook applies

1. Take the spawn call from the agent's before-tool hook.
2. Find the task or tasks the session concerns, in the same way the stop hook
   scopes them: the task's worktree or project directory first, otherwise the
   tasks this agent started.
3. If none of them has an item in progress with `subagents: false`, allow the
   spawn.
4. If the spawn's prompt contains the open assignment's bootstrap command with
   its token (`instruction <task> … --assignment <token>`), allow it. That's
   the manager handing out work, which ww drives.
5. Otherwise deny it, with a reason the model sees, for example: "`<step>` of
   `<task>` allows no subagents (`subagents: false`): do this work yourself."

The rule depends only on ww's state and the token, not on who is calling.
That's what makes it portable, since most agents don't tell a hook whether the
call comes from a subagent.

## Per-agent specifics (researched 2026-09-30, docs only)

| Agent | Spawn tool to match | Event and how to deny | Notes |
|---|---|---|---|
| Claude Code | `Agent` (alias `Task`) | `PreToolUse`: `hookSpecificOutput.permissionDecision: "deny"` + `permissionDecisionReason`, or exit 2 | Fires inside subagents, which carry `agent_id` and `agent_type`; nesting up to 3 levels |
| Codex | `spawn_agent` (matcher `Agent`) | `PreToolUse`, same JSON, or exit 2 | `SubagentStart` can't block; subagents use the parent's session id |
| Cursor | `Task` | `preToolUse`: `{"permission":"deny","agent_message":…}` (or `subagentStart`, which can deny) | Only one nesting level |
| Antigravity | `invoke_subagent` | `PreToolUse`: `{"decision":"deny","reason":…}` | Nothing identifies a call as coming from a subagent |
| Gemini CLI | one tool per subagent | `BeforeTool`: `decision: "deny"` + `reason` | Subagents can't spawn; only manager-performed steps need the hook |
| Grok Build | `spawn_subagent` | `PreToolUse`: `{"decision":"deny","reason":…}` | Depth 1; hook errors fail open |
| Kimi Code | `Agent`, `AgentSwarm` | `PreToolUse`, Claude-style JSON | Whether the hook fires inside subagents is explicitly unspecified |
| DeepSeek Harness | `dsh-tool-subagent` | `PreToolUse` through its Claude Code hooks bridge | Nesting allowed by design; developer preview |

Sources: code.claude.com/docs/en/hooks and /sub-agents;
learn.chatgpt.com/codex/hooks and /codex/agent-configuration/subagents;
cursor.com/docs/hooks and /subagents; antigravity.google/docs/hooks and
/subagents; github.com/google-gemini/gemini-cli docs/hooks/reference.md and
docs/core/subagents.md; docs.x.ai/build/features/hooks and /subagents;
kimi.com/code/docs (customization/hooks, customization/agents);
github.com/deepseek-ai/deepseek-harness (docs/subsystems/subagent.md,
packages/hooks/hooks-claude-code).

**First scope:** the four agents ww already installs hooks for: Claude Code,
Codex, Cursor and Antigravity. For the other four, the page instruction still
applies. On Gemini, Grok and Kimi their own no-nesting rule already covers
delegated steps.

## Design points

- **A new ww hook event** next to the existing session-start and stop events,
  for example `ww hook pre-spawn --agent <agent>`. It's registered by
  `ww hook install` for the spawn tool only, with the matcher from the table.
- **Needs TASK-22's assignment tokens**, to recognise ww's own dispatch.
- **When it can't decide, allow.** If the hook can't read the state or the task
  is unknown, it allows the spawn and doesn't break the session. `subagents`
  is a working rule, not a security boundary, the same stance as the tokens.
- **Record denials** in the task's audit record, so the operator can see a
  worker trying to spawn.

## Open question to settle first

What `cwd` does a subagent's hook report: the main session's directory, or the
worktree the worker switched into with `cd`? Test on Claude Code before
building. If it's the main session's directory, the hook falls back to "the
tasks this session concerns". Then, while any of them has a `subagents: false`
step in progress, non-dispatch spawns are refused for all of them.

## Tests

- Allow when no step is in progress with `subagents: false`.
- Allow the token-bearing dispatch.
- Deny other spawns, with the reason naming the step.
- Allow when the state is missing or the task unknown.
- Per agent: the native JSON parsed in, and the deny format written out.
- `ww hook install`, `uninstall` and `show` include the new registration.
- A task with `subagents: false` on a group covers every step inside it.

## Out of scope

- Hooks for Gemini, Grok, Kimi and DeepSeek.
- Renaming `loop_assignment`/`item_assignment` into one `assignment` key
  (postponed).
- The `group_assignment` idea ("one worker for a whole group").
