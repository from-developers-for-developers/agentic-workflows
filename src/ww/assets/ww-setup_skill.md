---
name: ww-setup
description: Guide the operator through setting ww up in this project, step by step, each step optional - ww learns about them, their team and company (ww-learn), learns how the project works (ww-learn-project), then suggests a gentle starting setup (ww-suggest). Use the first time ww is used in a project (`./ww discover` offers it while setup is not done), or when the operator asks to set up, onboard, or configure ww; on a later run it offers refreshing what ww learned, solving a problem, rules from past work, and automating a step.
---

# Set ww up with the operator

Talk to the operator in plain words, one question at a time, and ask every
question through your blocking question tool (`AskUserQuestion` in Claude
Code; `request_user_input` in Codex, never `request_user_input_async`, which
parks the question while you carry on), waiting for each answer before you
continue, or as a numbered list where there is none. Nothing changes without the operator seeing it first, and you never
edit ww's configuration files yourself: ww's workflows place changes with
`./ww setup apply`.

1. **Read the state.** Run `./ww discover` and `./ww onboarding --json`. If
   discover says ww is disabled, tell the operator and stop.
2. **Narration.** If `user.explain` is `null`, ask: "While ww learns, do you
   want me to explain what happens at each step?" (Yes / No), and record the
   answer: `./ww onboarding --set explain=true` (or `explain=false`). When it
   is `true`, add `--mode ww-narrate` to every `start` below and, between
   workflows, say in a sentence what comes next and why.
3. **Offer the steps.** When `project.setup.done` is `false`, explain the
   path in a few sentences, then ask which to do now (several may be picked;
   all three, in order, is the usual first choice):
   - `ww-learn`: a short interview about you, your team and your company.
     `me.md` stays on your machine; `team.md` and `company.md` go in `.ww/`
     and are shared with the team once committed.
   - `ww-learn-project`: reads how the project's work is organised (tooling,
     trackers, stack, conventions, recurring pitfalls) into `.ww/project.md`.
   - `ww-suggest`: proposes a small starting setup from all that, for you
     first, shared with the team only if you want.
   - None for now.

   When setup is already done, offer instead: refresh what ww learned (the
   `ww-refresh` skill), `ww-suggest` again (for example to share your setup
   with the team), `ww-solve` for a problem, `ww-rules-from-artifacts`, or
   `ww-automate`.
4. **Run each chosen workflow**, in the order above, with the start command
   `discover` shows:

   ```console
   ./ww start --workflow <name> --agent <agent> --requirements "<what the operator wants from it, in one line>" --role manager
   ```

   Omit the task ID unless `discover` says this project needs one; then ask
   the operator for it. Omit `--runtime`: each of these workflows chooses
   its own. Follow every page until the run completes. When a run offers the
   next workflow, start it only if the operator picked it in step 3 or says
   yes now.
5. **Finish.** Run `./ww onboarding --set setup.done=true`, also when the
   operator declined everything, so the offer is not repeated. Tell them what
   was written where, which shared files are left uncommitted for them to
   review and commit (`.ww/team.md`, `.ww/company.md`, `.ww/project.md`,
   and the setup files `ww-setup.yaml`, `ww.yaml` and
   `ww.json` when a setup was shared), and that `/ww-setup`
   can be run again any time.
