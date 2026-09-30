---
name: ww-automate
description: Analyse whether a workflow step's manual, text-based work could be done by a script ww runs, and propose the script and the handler change; applies them once the operator confirms. Use when the operator invokes /ww-automate or asks whether a step could be automated or scripted.
---

# Automate a step's mechanical work

The `ww-automate` workflow asks which step to look at, analyses its
instruction and past results, proposes a script and the handler that runs
it, and places the handler with `./ww setup apply` once the operator
confirms; this skill starts it.

1. Run `./ww onboarding --json`. If `user.explain` is `true`, add
   `--mode ww-narrate` below.
2. Start it with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one:

   ```console
   ./ww start --workflow ww-automate --agent <agent> --requirements "<the step to look at, if the operator named one>" --role manager
   ```

3. Follow every page until the run completes. Never edit ww's configuration
   files yourself.
