---
name: ww-refresh
description: Refresh what ww learned about the operator, their role in the project, team, company or project, keeping what still holds, updating what changed and marking what no longer holds as superseded. Use when the operator invokes /ww-refresh or says ww's picture of them, their team or the project is out of date.
---

# Refresh what ww learned

Refreshing is running the learning workflows again: their steps read the
existing files first and update them in place.

1. Run `./ww onboarding --json` and tell the operator when ww last learned
   about each subject (`learned.me`, `learned.myrole`, `learned.team`,
   `learned.company`, `learned.project`; `null` is never).
2. Ask through your question tool, ending your turn until the answer comes,
   what to refresh (several may be picked): "me", "my role in this project",
   "my team and company", "the project". The first three are the `ww-learn`
   workflow: start it once and answer its `choose` step to match ("only me",
   "only my role", "only team and company", or "everything" when all three
   are picked). "The project" is `ww-learn-project`.
3. If `user.explain` is `true`, add `--mode ww-narrate`. Start each chosen
   workflow with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one:

   ```console
   ./ww start --workflow <ww-learn or ww-learn-project> --agent <agent> --requirements "Refresh what ww knows; keep what still holds." --role manager
   ```

4. Follow every page until each run completes. When `ww-learn` offers
   `ww-learn-project` next, start it only if the operator also chose the
   project.
