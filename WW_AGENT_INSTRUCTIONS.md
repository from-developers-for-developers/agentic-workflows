# Working with ww

ww saves progress, runs handlers, and names the next role; it is only for
development. Where ww is used by default, every file change goes through ww
unless the user asks you not to. Reviews, questions, investigations and other
read-only work never use ww, its skill or a task.

## Start here

Before starting new work, run:

```console
./ww discover
```

It lists the project's workflows, modes, the start command, and
whether to use ww unasked. If disabled, stop; if used only on request, use it only when
the user asks for ww. Choose the matching workflow and keep its default modes
unless the request matches another mode; ask only when the choice would
materially change the work.

When a request names an external ticket, use that as the task ID. Omit the ID
only when none is named or the workflow obtains it in its first step.

A request that names a ticket or clearly matches a workflow's description, and
is more than a small change: propose that workflow, and offer the alternative in
the same choice, "run `<workflow>`" / "just do it, register afterwards". Any
other request is direct work: do it as in a plain conversation, ask nothing,
then run `./ww record <task> --summary "..."` (`./ww lookup [<task>] --agent
<agent>` names the task). Judge each prompt on its own; a series of small
requests stays direct work, never a workflow. Ask only when genuinely ambiguous,
and never again on a task once the operator chose direct work. If `record`
fails or is forgotten, ww records the commits on the next manager
page (`instruction --role manager`): do not retry.

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
- On nonzero exit, read the full response. For "Fix required", repair and complete again
  (`./ww check <task-id>` previews checks) or `./ww dispute`; record impossible work
  with `./ww fail` and the page's role/assignment. On `awaiting_operator`, stop and
  report the reason and exact error; run only the operator's chosen recovery option;
  never reset unasked. A standing operator authorization for routine repairs (dependency
  installation, formatting, retries) covers such stops: apply it, asking only for a
  material decision or an action outside it.
- Relay feedback-deduction suggestions after completion; use its skill and ww
  commands. Rule approval and pruning are separate follow-ups.
