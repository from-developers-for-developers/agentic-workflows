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

3. Follow every page until the run completes. Each interview opens with its
   questions in one numbered message, then is a conversation: follow up
   where an answer deserves it and say what it implies. Treat clear contextual
   completion as permission to finish; ask naturally if it is ambiguous. Then
   record it once with the page's transcript command and go on. Ask the
   operator through a suitable choice tool wherever a page offers choices.
   Inspect the host's available question-tool schema and follow its supported
   fields, using structured options when offered and a text-only question only
   when required. Keep the choice pending until the operator explicitly
   answers; a timeout, dismissal, or preselected value is not an answer. If no
   suitable tool is available, ask as a numbered list in chat. End your turn
   after asking and do nothing that depends on the answer until it arrives.
