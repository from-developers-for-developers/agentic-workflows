# Working with ww

ww saves progress, runs handlers, and names the next role. Where ww is used by
default, use it for project work unless the user asks you not to; every file
change goes through ww. When no workflow fits, run `./ww lookup [<task>] --agent
<agent>` with the task in context and follow it; it never creates a task without
operator confirmation. Questions and other read-only work need no task.

## Start here

Before starting new work, run:

```console
./ww discover
```

It lists the project's workflows, modes, the start command, resume commands, and
whether to use ww unasked. If disabled, stop; if used only on request, use it only when
the user asks for ww. Choose the matching workflow and keep its default modes
unless the request matches another mode; ask only when the choice would
materially change the work.

When a request names an external ticket, use that as the task ID. Omit the ID
only when none is named or the workflow obtains it in its first step.

To continue an existing task, run `./ww instruction <task-id> --role manager`;
a worker resuming its assignment uses `--role worker`. Check status with
`./ww status <task-id>`.

## Rules
- Follow each authoritative ww page: replace placeholders and supply requested
  values until completion, error, or assignment end. One completion rarely ends a task.
- Perform only agent-owned work. Never run or bypass a ww handler, edit ww state, or read `ww.yaml` or ww source to infer the next step.
- For handler repair, fix the cause and complete with an artifact; ww retries it. In `auto`, the manager dispatches repairs with `next`.
- On an `interactive: true` step, converse in the operator session and finish
  on clear contextual intent; ask if it is ambiguous. "Done for today" means
  pause. Record both sides and end the interaction before completing it.
- Use a host choice tool when available, following its contract; otherwise use
  a numbered chat list. Keep async choices pending until an answer arrives;
  timeout, dismissal, and preselection are not answers.
- On nonzero exit, read the full response. For "Fix required", repair and complete
  again (`./ww check <task-id>` previews checks) or dispute with `./ww dispute`;
  record impossible work with `./ww fail` and the page's role/assignment. On
  `awaiting_operator`, stop and report the reason and exact error. Run only the
  operator's chosen recovery option; never reset unasked.
- Relay feedback-deduction suggestions after completion; use its skill and ww
  commands. Rule approval and pruning are separate follow-ups.
