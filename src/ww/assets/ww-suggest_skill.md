---
name: ww-suggest
description: Propose a small, gentle ww setup (workflows, modes, a few simple rules, hooks, settings) from what ww learned about the operator, team, company and project, and place it for the operator alone or shared with the team. Use when the operator invokes /ww-suggest, asks what ww setup would suit them, or wants to share their own ww setup with the team.
---

# Suggest a ww setup

The `ww-suggest` workflow reads `me.md`, `team.md`, `company.md` and
`project.md`, asks whether to set ww up for the operator and whether to share
it, shows the proposal, and places it with `./ww setup apply`; this skill
starts it. Running it again later can share a setup tried alone with the team.

1. Run `./ww onboarding --json`. If `user.explain` is `true`, add
   `--mode ww-narrate` below. If nothing was learned yet (no `learned.*`
   timestamps), say that suggestions will be generic and offer the
   `ww-learn` and `ww-learn-project` skills first.
2. Start it with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one:

   ```console
   ./ww start --workflow ww-suggest --agent <agent> --requirements "Suggest a starting ww setup." --role manager
   ```

3. Follow every page until the run completes. Never edit ww's configuration
   files yourself; the workflow places the setup with `./ww setup apply`.
