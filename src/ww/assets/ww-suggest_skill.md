---
name: ww-suggest
description: Design a ww setup with the operator from what ww learned about the project, and propose it in full, following ww's design guidance, placed for the operator alone or shared with the team. Use when the operator invokes /ww-suggest, asks what ww setup would suit them, or wants to share their own ww setup with the team.
---

# Suggest a ww setup

The `ww-suggest` workflow reads `project.md` and ww's design documents
(`./ww docs specification`, `./ww docs features`, `./ww docs examples`), asks the operator a few questions about their
process (skipped for an express setup, which derives defaults from the
project), settles in one set of questions whose defaults come from the
project's profile what the setup turns on and for whom, shows the proposal section by section with the
evidence for each piece and a walkthrough of the main lane, and places it
with `./ww setup apply`; this skill starts it. Running it again later can share a setup tried alone with the team. For each workflow it proposes it states the trigger, the result and where the operator is involved, picks the smallest structure, shows YAML with a walkthrough (and a failure path where effects are external), validates and inspects the compiled plan before asking, and takes test commands from repository evidence only. To change a workflow that already exists, or to pick between branches of work, the `ww-wizard` skill is the better start.

1. Run `./ww onboarding --json`. If `user.explain` is `true`, add
   `--mode ww-narrate` below. If the project was not learned yet (no `learned.project`
   timestamp), say that suggestions will be generic and offer the
   `ww-learn-project` skill first.
2. Start it with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one:

   ```console
   ./ww start --workflow ww-suggest --agent <agent> --requirements "Design and propose a ww setup for this project." --role manager
   ```

   For an express setup, start the requirements with "Express setup." so
   that the workflow derives defaults and asks only what the project leaves
   open.

3. Follow every page until the run completes. Never edit ww's configuration
   files yourself; the workflow places the setup with `./ww setup apply`.
