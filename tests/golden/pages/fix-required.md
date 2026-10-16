# TASK-1 · task

## Fix required: 1 of 1 checks failed (attempt 1 of 3 for `develop/sh`)

ww did not record your completion: the checks below failed on the files this step changed. The step is still yours. Each check counts its own failures; the step stops for the operator when any one reaches its limit.

### `develop/sh` (hook)

Failures: 1 of 3 allowed.
Command: sh -c 'test ! -e broken'
Output: nothing

Fix the causes, then complete again with the whole result as the artifact: it replaces the draft you submitted entirely; `./ww check TASK-1` previews the checks.

If a check is wrong for this change, do not work around it: dispute it with the evidence, and the operator decides; repeat `--rule` for several checks:

```console
./ww dispute TASK-1 --role worker --rule <id> --reason "<why the check is wrong here>"
```

### Worker completion command

```console
./ww complete TASK-1 --role worker --artifact="<whole result in Markdown>" --summary="<one or two sentences for the next step>"
```
