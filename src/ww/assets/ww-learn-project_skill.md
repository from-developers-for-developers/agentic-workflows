---
name: ww-learn-project
description: Let ww learn how this project's work is organised - the setup facts (branching, exact verify commands, tracker key format, commit convention, CI gates), agent tooling and MCP servers, issue trackers, infrastructure and stack, conventions, and recurring pitfalls from history and reviews - and its purpose, into .ww/project.md; an existing file is refreshed, not replaced. Use when the operator invokes /ww-learn-project or asks ww to learn, or relearn, the project.
---

# Let ww learn how the project works

The `ww-learn-project` workflow starts from `./ww inspect`'s read-only
profile of the checkout, reads only what the profile cannot see, shows the
operator what it found, and writes `.ww/project.md`; this skill starts it.

1. Run `./ww onboarding --json`. If `user.explain` is `true`, add
   `--mode ww-narrate` below.
2. Start it with the start command `./ww discover` shows, omitting the task
   ID unless discover says this project needs one:

   ```console
   ./ww start --workflow ww-learn-project --agent <agent> --requirements "Learn how this project's work is organised." --role manager
   ```

3. Follow every page until the run completes. The scan only reads; it
   changes no project file.
