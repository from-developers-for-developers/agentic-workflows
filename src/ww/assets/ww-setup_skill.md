---
name: ww-setup
description: Guide the operator through setting ww up in this project, express or guided - ww learns the repository (ww-learn-project), then designs a minimal setup with the operator and proposes it (ww-suggest), which in the guided path asks a few questions about their process and in the express path derives defaults from the project. Use when the operator asks to set up, onboard, or configure ww, or accepts the offer `./ww discover` makes while setup is not done; on a later run it offers refreshing what ww learned, solving a problem, rules from past work, and automating a step.
---

# Set ww up with the operator

Talk to the operator in plain words. ww learns the project and asks only
about the work and the process, never about who the operator is, their role,
their team or their company. A set of questions opens in one numbered
message, each a plain sentence with its options listed beneath where there
are a few, then becomes a conversation: follow up where an answer deserves it
and reason aloud about what it implies for the setup, until the operator's
intent to finish is clear; respond to corrections and questions, and ask
naturally if that intent is ambiguous. Only then record it, once, with the
transcript command the step's page shows, and go on. Setup is optional and
never blocks ordinary work. A single choice, as in the `choose` steps, goes through your native
choice tool where available. Inspect the host's available question-tool schema
and follow its supported fields, using structured options when offered and a
text-only question only when required. Keep the choice pending until the
operator explicitly answers; a timeout, dismissal, or preselected value is not
an answer, and no dependent step may proceed. If there is no suitable question
tool, ask in the chat as a numbered list of options. End your turn after
asking and resume only when the operator answers. The operator sees every
file ww writes, the setup before it is placed, and you never edit ww's
configuration files yourself: ww's workflows place changes with
`./ww setup apply`.

1. **Read the state.** Run `./ww discover` and `./ww onboarding --json`. If
   discover says ww is disabled, tell the operator and stop. Note whether
   `.ww/project.md` exists and when `project.learned.project` says ww last
   learned the project (`null` is never).
2. **Ask once.** Put everything into one opening message (all questions at
   once in your question tool where it takes several, as `AskUserQuestion`
   does), and end your turn. When `project.setup.done` is `false`, it asks
   which path to take:
   - Express: ww learns the repository, then derives a setup from it and asks
     you only for a consequential choice the project does not settle. Runs
     `ww-learn-project`, then `ww-suggest` with the requirement "Express
     setup".
   - Guided: ww learns the repository, then asks a few questions about your
     process: what is painful, what outcome would help, where you want to be
     involved and what may run automatically, skipping what the project
     already answers. Runs `ww-learn-project`, then `ww-suggest`.
   - None for now.

   Explain the path in a few sentences: `ww-learn-project` records the
   project's purpose, stack, verify commands, CI, review and release process,
   conventions and recurring pitfalls, starting from `./ww inspect`, in
   `.ww/project.md`, which is shared with the team once committed; when that
   file exists, it is refreshed, not replaced; `ww-suggest` designs a minimal
   setup from it with you, grounded in ww's design documents (`./ww docs
   specification|features|examples`), proposes it in full with the evidence for each piece, walks
   through a realistic task, revises it from your feedback, validates it, and
   places it for you alone or shared with the team. The guided path takes one
   more reply than the express one: your answers about the process.

   Narration is optional and is never asked here. If the operator says they
   want to see what ww does while it works, record it:
   `./ww onboarding --set explain=true` (`explain=false` to stop). While
   `user.explain` is `true`, add `--mode ww-narrate` to every `start` below
   and, between workflows, say in a sentence what comes next and why.
3. **Name what is not learned yet.** In the same message, when `project.md`
   is missing or `learned.project` is `null`, say the project is not learned
   yet and recommend `ww-learn-project`, also when `project.setup.done` is
   already `true`. When it exists, offer to refresh it only if the operator
   says it is out of date; do not ask again what it already records.

   When setup is already done, offer: refresh what ww learned (the
   `ww-refresh` skill), `ww-suggest` again (for example to share your setup
   with the team), `ww-wizard` to create or change a workflow or rules,
   `ww-solve` for a problem, `ww-rules-from-artifacts`,
   `ww-automate`, or `ww-scriptize` to turn the rules that have no check yet
   into checks.
4. **Run each chosen workflow**, in the chosen path's order, with the start command
   `discover` shows:

   ```console
   ./ww start --workflow <name> --agent <agent> --requirements "<what the operator wants from it, in one line>" --role manager
   ```

   For the express path, the `ww-suggest` requirements start with "Express
   setup." so that it does not ask the process questions. Omit the task ID
   unless `discover` says this project needs one; then ask the operator for
   it. Omit `--runtime`: each of these workflows chooses its own. Follow every
   page until the run completes. When a run offers the next workflow, start
   the next one of the chosen path, or another only if the operator says yes
   now.
5. **Finish.** Run `./ww onboarding --set setup.done=true`, also when the
   operator declined everything, so the offer is not repeated. Tell them what
   was written where, which shared files are left uncommitted for them to
   review and commit (`.ww/project.md`, and the setup files `ww-setup.yaml`,
   `ww.yaml` and `ww.json` when a setup was shared), and that `/ww-setup`
   can be run again any time.
