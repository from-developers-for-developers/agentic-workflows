# TASK-1 · task

> Completion recorded successfully by `ww`.

## Worker: perform `update-workflow-summary`

Same assignment continues: next item `update-workflow-summary`. Do not return to the manager yet. Perform it and run the displayed worker completion command.

### Previous step result

The `review` step left this summary for you:

> The change is fine.

Its full result, when you need more: `<root>/.ww/tasks/TASK-1/runs/01-task/steps/03-review.md`

### Work instruction

Provide a concise summary of this workflow run's goal and result.

> **No subagents.** This step allows no subagents (`subagents: false`): whoever performs it does all of its work alone and spawns no subagent for anything, not for research, tests, or review.

### Step handovers of this run

Build the summary from these handovers, in order; each names its artifact for detail. State only what they, or the artifacts you actually read, contain: no counts, test results, or statuses from anywhere else, and nothing from other runs of this task.

- `develop`: Added the flag. (artifact: `<root>/.ww/tasks/TASK-1/runs/01-task/steps/02-develop.md`)
- `review`: The change is fine. (artifact: `<root>/.ww/tasks/TASK-1/runs/01-task/steps/03-review.md`)

### Worker completion command

Values to supply in place of their `<...>` placeholders:

- `summary` — A short goal/result summary for the task workflow ledger.

```console
./ww complete TASK-1 --role worker --assignment 0a1b2c3d --variable summary="<summary>" --artifact="<whole result in Markdown>"
```
