---
name: ww-learn
description: Let ww learn about the operator, their role in this project, their team and their company through a short interview, into me.md (personal), myrole.md (personal to this checkout, git-ignored in .ww) and team.md and company.md (shared in .ww). Use when the operator invokes /ww-learn or asks ww to learn, or relearn, about them, their role, their team, or their company.
---

# Let ww learn about you, your role, your team and your company

The `ww-learn` workflow asks the questions and writes the files; this skill
starts it.

1. Run `./ww onboarding --json`. If `user.explain` is `true`, add
   `--mode ww-narrate` below.
2. Start it with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one:

   ```console
   ./ww start --workflow ww-learn --agent <agent> --requirements "Learn about the operator, their role in this project, their team and their company." --role manager
   ```

3. Follow every page until the run completes. Ask the operator through your
   blocking question tool wherever a page offers choices, or as a numbered
   list in the chat when you have only an asynchronous one (Codex outside
   plan mode), and end your turn right after asking.
