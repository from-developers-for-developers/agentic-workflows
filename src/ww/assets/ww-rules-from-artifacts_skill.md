---
name: ww-rules-from-artifacts
description: Read what chosen workflow steps produced across past ww tasks (their artifacts) and propose rules from the lessons that recur, added through ww's rule commands once the operator confirms. Use when the operator invokes /ww-rules-from-artifacts or asks to learn rules from past reviews, fixes, or other step results.
---

# Learn rules from past step results

The `ww-rules-from-artifacts` workflow asks which steps to learn from, reads
their artifacts across recent tasks, proposes a few rules, and adds the
accepted ones with `./ww rules add`; this skill starts it.

1. Run `./ww onboarding --json`. If `user.explain` is `true`, add
   `--mode ww-narrate` below.
2. Start it with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one:

   ```console
   ./ww start --workflow ww-rules-from-artifacts --agent <agent> --requirements "Propose rules from what past steps produced." --role manager
   ```

3. Follow every page until the run completes. Never edit a rule file or
   ww's configuration files yourself.
