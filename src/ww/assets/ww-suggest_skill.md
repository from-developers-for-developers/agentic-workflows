---
name: ww-suggest
description: Design a ww setup with the operator from what ww learned about them, their role, team, company and project, and propose it in full (git branching, handlers with the project's verify commands, workflows per lane, modes, rules only for what a command cannot check), placed for the operator alone or shared with the team. Use when the operator invokes /ww-suggest, asks what ww setup would suit them, or wants to share their own ww setup with the team.
---

# Suggest a ww setup

The `ww-suggest` workflow reads `me.md`, `myrole.md`, `team.md`,
`company.md` and `project.md`, settles with the operator in one set of
questions whose defaults come from the project's profile what the setup
turns on and for whom, shows the proposal section by section with the
evidence for each piece and a walkthrough of the main lane, and places it
with `./ww setup apply`; this skill starts it. Running it again later can share a setup tried alone with the team.

1. Run `./ww onboarding --json`. If `user.explain` is `true`, add
   `--mode ww-narrate` below. If nothing was learned yet (no `learned.*`
   timestamps), say that suggestions will be generic and offer the
   `ww-learn` and `ww-learn-project` skills first.
2. Start it with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one:

   ```console
   ./ww start --workflow ww-suggest --agent <agent> --requirements "Design and propose a ww setup for this project." --role manager
   ```

3. Follow every page until the run completes. Never edit ww's configuration
   files yourself; the workflow places the setup with `./ww setup apply`.
