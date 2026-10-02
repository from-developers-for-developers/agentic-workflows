---
name: ww-setup
description: Guide the operator through setting ww up in this project, express or guided - ww learns how the project works (ww-learn-project), then infers the operator, their role, team and company from the repository for one confirmation (ww-express) or learns them in short interviews (ww-learn), then designs a setup with the operator and proposes it (ww-suggest). Use the first time ww is used in a project (`./ww discover` offers it while setup is not done), or when the operator asks to set up, onboard, or configure ww; on a later run it offers refreshing what ww learned, solving a problem, rules from past work, and automating a step.
---

# Set ww up with the operator

Talk to the operator in plain words. An interview opens with all of a
subject's questions in one numbered message, each a plain sentence with its
options listed beneath where there are a few, then becomes a conversation:
follow up where an answer deserves it and reason aloud about what it implies
for the setup, until the operator says `ww done` or is clearly done. Only
then record it, once, with the transcript command the step's page shows, and
go on. A single choice, as in the
`choose` steps, goes through your blocking question tool where you have one
(`AskUserQuestion` in Claude Code; `request_user_input` in Codex's plan
mode). Either way, end your turn right after asking: do nothing else until
the operator has answered. Where the only question tool is an asynchronous one
(Codex outside plan mode offers `request_user_input_async`), do not use it:
its form disappears when your turn ends. Ask in the chat instead, as a
numbered list of the options, and end your turn. The operator sees every
file ww writes, the setup before it is placed, and you never edit ww's
configuration files yourself: ww's workflows place changes with
`./ww setup apply`.

1. **Read the state.** Run `./ww discover` and `./ww onboarding --json`. If
   discover says ww is disabled, tell the operator and stop.
2. **Ask once.** Put everything into one opening message (all questions at
   once in your question tool where it takes several, as `AskUserQuestion`
   does), and end your turn. When `project.setup.done` is `false`, it asks
   which path to take:
   - Express: ww learns the project, infers your profile, your role, your
     team and your company from the repository, and asks you to confirm.
     Runs `ww-learn-project`, then `ww-express`, then `ww-suggest`.
   - Guided: ww learns the project, then short interviews about you, your
     role, your team and your company. Runs `ww-learn-project`, then
     `ww-learn`, then `ww-suggest`.
   - None for now.

   Explain the path in a few sentences: `ww-learn-project` profiles how the
   project's work is organised with `./ww inspect` (branching, activity,
   fixes, verify commands, tracker, commit convention) and writes it into
   `.ww/project.md`; `ww-express` or `ww-learn` writes `me.md`, which stays
   on your machine, `.ww/myrole.md`, which stays in this checkout, out of
   version control, and `.ww/team.md` and `.ww/company.md`, which are shared
   with the team once committed; `ww-suggest` designs the setup with you,
   each default taken from the profile (lanes, the commands that verify a
   change, who reviews, your preferences as modes), and proposes it in full
   with the evidence for each piece, for you alone or shared with the team.
   Express takes five replies: this message, the project review, the
   confirmation, the design and "apply".

   Ask about narration in the same message, only while `user.explain` is
   `null`: "While ww learns, do you want me to explain what happens at each
   step?" (Yes / No). Record the answer: `./ww onboarding --set explain=true`
   (or `explain=false`). When it is `true`, add `--mode ww-narrate` to every
   `start` below and, between workflows, say in a sentence what comes next
   and why.
3. **Name what is not learned yet.** In the same message, list the subjects
   whose `learned.*` is `null` (`me`, `myrole`, `team`, `company`, `project`)
   as "not learned yet" and recommend learning them first,
   also when `project.setup.done` is already `true`, naming what covers them:
   `ww-express` for all of me, myrole, team and company, or `ww-learn` with
   "only me" for me, "only my role" for myrole, "only team and company" for
   team or company, and "everything" when they fall under more than one of
   these; `ww-learn-project` for project.

   When setup is already done, offer, after any subject not learned yet:
   refresh what ww learned (the `ww-refresh` skill), `ww-suggest` again (for
   example to share your setup with the team), `ww-solve` for a problem,
   `ww-rules-from-artifacts`, or `ww-automate`.
4. **Run each chosen workflow**, in the chosen path's order, with the start command
   `discover` shows:

   ```console
   ./ww start --workflow <name> --agent <agent> --requirements "<what the operator wants from it, in one line>" --role manager
   ```

   For `ww-learn`, the requirements name what to cover, such as "Cover
   everything." or "Cover only my role.", the choice step 3 recommended
   unless the operator said otherwise, so that its `choose` step does not ask
   again. Omit the task ID unless `discover` says this project needs one;
   then ask the operator for it. Omit `--runtime`: each of these workflows chooses
   its own. Follow every page until the run completes. When a run offers the
   next workflow, start the next one of the chosen path, or another only
   if the operator says yes now.
5. **Finish.** Run `./ww onboarding --set setup.done=true`, also when the
   operator declined everything, so the offer is not repeated. Tell them what
   was written where, which shared files are left uncommitted for them to
   review and commit (`.ww/team.md`, `.ww/company.md`, `.ww/project.md`,
   and the setup files `ww-setup.yaml`, `ww.yaml` and
   `ww.json` when a setup was shared), and that `/ww-setup`
   can be run again any time.
