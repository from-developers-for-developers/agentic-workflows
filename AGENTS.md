# Working with ww

This project coordinates work through ww: it saves progress, runs automatic
handlers, and tells you which role acts next. Use it for requests to implement,
fix, investigate, review, or otherwise carry out project work, unless the user
asks you not to. Ordinary questions need no task.

## Start here

Before starting new work, run:

```console
./ww discover
```

It says whether ww is enabled here, lists the workflows, modes, runtimes, and
start options to choose from, and shows the exact commands. If ww is disabled,
do not use it. Choose the workflow that matches the request, keep its default
modes unless the user's wording matches another mode, and ask the user only
when the choice would materially change the work.

To continue an existing task instead of starting another, run
`./ww instruction <task-id> --role manager`; a worker resuming its assignment
uses `--role worker`. `./ww status <task-id>` is a quick state check.

## Rules

- Each ww response is authoritative. Run the displayed commands with every
  placeholder replaced, supply what they ask for, and continue until ww
  reports that the workflow is complete or reports an error. One completion
  rarely finishes the task.
- Perform only agent-owned work. Never run or work around a ww-owned handler,
  edit ww state, or read `ww-agentic-workflows.yaml` or ww's source to reconstruct what
  happens next.
- On a nonzero exit, read the whole response. If agent work cannot finish,
  record it with `./ww fail <task-id> --role worker --error "<reason>"`. If an
  automatic handler fails, stop and report the task and the exact error to
  the user; recovery is their decision. Never reset a task unless asked.

## Codex execution boundary

Run WW CLI commands using Codex's normal sandbox policy. Do not request
escalation preemptively.

If a specific WW command fails because sandbox access prevents it from
completing, rerun only that command outside the sandbox with approval. WW
remains the trusted execution boundary for its handlers; do not invoke its Git
or other handlers separately.

@WW_AGENT_INSTRUCTIONS.md
