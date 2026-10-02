# Working with ww

This project coordinates work through ww: it saves progress, runs automatic
handlers, and tells you which role acts next. Where ww is used by default, use
it for requests to implement, fix, investigate, review, or otherwise carry out
project work unless the user asks you not to, and every change to files goes
through ww: when no workflow fits, run `./ww lookup [<task>] --agent <agent>`
with the task in context and follow it; it never creates a task without the
operator's confirmation. Questions and other read-only work need no task.

## Start here

Before starting new work, run:

```console
./ww discover
```

It lists the workflows, modes, runtimes, start options, and exact commands, and
it says whether to use ww unasked. If ww is disabled, do not use it; if it is
used only on request, use it only when the user explicitly asks for ww. Choose
the matching workflow, keep its default modes unless the user's wording matches
another mode, and ask only when the choice would materially change the work.

When the request names an external ticket, such as a Jira key, start the task
under that key so the task ID matches the issue. Omit the task ID only when the
request names none, or when the workflow obtains its own ID in its first step.

To continue an existing task instead of starting another, run
`./ww instruction <task-id> --role manager`; a worker resuming its assignment
uses `--role worker`. `./ww status <task-id>` is a quick state check.

## Rules

- Each ww response is authoritative. Run the displayed commands with every
  placeholder replaced, supply what they ask for, and continue until ww
  reports the workflow complete, an error, or that your assignment ended,
  then stop. One completion rarely finishes the task.
- Perform only agent-owned work. Never run or work around a ww-owned handler,
  edit ww state, or read `ww.yaml` or ww's source to reconstruct what
  happens next.
- On a nonzero exit, read the whole response. On "Fix required", fix the causes
  and complete again (`./ww check <task-id>` previews checks) or `./ww dispute`
  a wrong check; `./ww fail` with the page's `--role` and `--assignment`
  records work that cannot finish. On `awaiting_operator` (a failure, limit,
  interruption, checks or changed workflow), stop, report the task,
  `operator_reason` and the exact error to the user, and run only the `./ww next`
  option they pick (`--retry`, `--force`, `--approve`, `--approach`, `--pick`,
  `--replan`, `--keep-plan`, `--yes` once they said so); never reset unasked.
