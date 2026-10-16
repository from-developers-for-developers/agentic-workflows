# TASK-1 · task

## Manager and worker: perform `develop`

You play both roles in this session: act as the worker and do the work yourself, not through a subagent.

### Task requirements

Add a --verbose flag to the CLI.

### Sent back by a goto

`review` sent the flow back to this step (goto round 1). This is not a fresh start: this step's earlier result is among the previous artifacts. Rework it for the reason below, then complete as usual; the steps after this one, up to `review`, run again too.

Why: `review`'s result, below.

### Previous step result

The `review` step left this summary for you:

> Document the flag in the CLI help.

Its full result, when you need more: `<root>/.ww/tasks/TASK-1/runs/01-task/steps/03-review.md`

### Work instruction

Implement the change described in the requirements.

### Worker completion command

```console
./ww complete TASK-1 --role worker --artifact="<whole result in Markdown>" --summary="<one or two sentences for the next step>"
```
