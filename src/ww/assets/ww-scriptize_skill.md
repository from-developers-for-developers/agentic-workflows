---
name: ww-scriptize
description: Turn the project's rules that have no check yet into checks, proven and approved, with ww's ww-scriptize-rules workflow, on a branch of their own. Use when the operator invokes /ww-scriptize, asks to scriptize or automate the checking of rules, or when ww says rules are not scriptized yet.
---

# Scriptize the project's rules

The `ww-scriptize-rules` workflow lists the rules that have no check yet,
agrees the checks with the operator, builds and proves them in its own
workspace, and records the approved ones with `./ww rules convert` once the
operator confirms. It automatically creates a branch from
`extensions.ww/git.base_branches.default` and follows ww/git's worktree
settings; this skill starts it.

1. Run `./ww discover`. If it does not list `ww-scriptize-rules`, the
   operator switched it off; say so and stop.
2. Start it with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one:

   ```console
   ./ww start --workflow ww-scriptize-rules --agent <agent> --requirements "<which rules to scriptize, if the operator named some; else all that need it>" --role manager
   ```

   The ww/git default base branch must be configured; its absence is an
   error. Never edit ww's configuration files yourself.
3. Follow every page until the run completes. Tell the operator that the
   checks run in tasks only where their configuration files exist, so once
   this run's branch is merged, and that `./ww rules convert` and
   `./ww rules decline` wrote `ww-rule-automation.json` at the project root,
   in the main checkout: they commit it there, on the integration branch,
   together with or right after merging this run's branch. Until then the
   next task's start is refused by `is-git-clean`, which finds the checkout
   changed.
