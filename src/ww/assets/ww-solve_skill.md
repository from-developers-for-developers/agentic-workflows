---
name: ww-solve
description: Listen to a problem the operator has with how work goes with agents or ww, and propose workflows, modes, rules or hooks that address it, based on what ww learned; applies them once the operator confirms. Use when the operator invokes /ww-solve or describes a recurring problem they want ww to help prevent.
---

# Solve a problem with ww's setup

The `ww-solve` workflow listens, proposes the smallest change, shows it, and
places it with `./ww setup apply` once the operator confirms; this skill
starts it.

1. Run `./ww onboarding --json`. If `user.explain` is `true`, add
   `--mode ww-narrate` below.
2. Start it with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one, and the problem as the
   operator put it:

   ```console
   ./ww start --workflow ww-solve --agent <agent> --requirements "<the problem, in the operator's words>" --role manager
   ```

3. Follow every page until the run completes. Never edit ww's configuration
   files yourself.
