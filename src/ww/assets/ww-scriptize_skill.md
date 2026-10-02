---
name: ww-scriptize
description: Turn the project's rules that have no check yet into checks, proven and approved, with ww's ww-scriptize-rules workflow, on a branch of their own. Use when the operator invokes /ww-scriptize, asks to scriptize or automate the checking of rules, or when ww says rules are not scriptized yet.
---

# Scriptize the project's rules

The `ww-scriptize-rules` workflow lists the rules that have no check yet,
agrees the checks with the operator, builds and proves them in its own
workspace, and records the approved ones with `./ww rules convert` once the
operator confirms. It runs with a project lane's branch, worktree and commit
handling; this skill starts it.

1. Run `./ww discover`. If it does not list `ww-scriptize-rules`, the
   operator switched it off; say so and stop.
2. Start it with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one:

   ```console
   ./ww start --workflow ww-scriptize-rules --agent <agent> --requirements "<which rules to scriptize, if the operator named some; else all that need it>" --role manager
   ```

   If `start` refuses because no lane is named for the workflow's hooks, show
   the operator its message and ask which workflow's branch, worktree and
   commit handling it should use (usually the main lane, such as `task`).
   Tell them the line to add to `ww.json`; never edit ww's configuration
   files yourself.
3. Follow every page until the run completes. Tell the operator that the
   checks run in tasks only where their configuration files exist, so once
   this run's branch is merged.
