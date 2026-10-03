---
name: ww-feedback-rules
description: Review learned operator-feedback candidates and propose lasting ww rules when the operator invokes /ww-feedback-rules or asks to turn learned feedback into rules.
---

# Turn learned feedback into approved rules

Run `./ww feedback --json`, `./ww rules --json`, and `./ww discover`.
Review every candidate, its evidence, recurrence rationale, occurrence count,
task ratios, `last_encountered_at`, and suggested enforcement. Candidates are suggestions, never
instructions or permission to act. Do not require a minimum frequency: one
occurrence can justify a rule when the reasoning supports recurrence. Explain
which candidates warrant a rule and which do not; do not treat a ratio as a
prediction. Feedback may reflect missing requirements rather than a mistake.

For each worthwhile candidate, match existing rules by meaning. Propose a new
rule or an amendment with one imperative obligation, real workflow/step
filters, relevant file globs, rationale and an example. Assess scripted versus
reasoning enforcement now: name a concrete check approach if mechanical, or
explain what judgement a verifier must apply. Use the project's check guidance.
Do not install an untested script just because the candidate says scripted.

Show the full proposed batch, including existing wording for amendments,
placement, scope, enforcement and supporting feedback IDs. Ask the operator
which proposals to approve and wait for their answer. Invoking this skill
requests a review, not automatic installation. If none merit a rule, explain
why and proceed to pruning. Do not ask during ordinary interactive rounds unless the
operator has requested this review.

For approved proposals only, use `./ww rules add --group <name> --dir <path>
[--workflows ...] [--steps ...]` for any new group, then
`./ww rules add <group> --text "<sentence and rationale>" [--paths <glob> ...]`
or `./ww rules edit <id> --text "<sentence and rationale>" [--paths <glob> ...]`.
Run `./ww rules --help` for options, and use `--dry-run` to validate the
concrete changes before writing. Do not edit rule files or the feedback store
by hand. When amending a rule with a stored check, account for its wording
hash: propose promotion of an unchanged check, or revalidation for changed
meaning. Use `ww-scriptize` when a new mechanical check needs development.

After reviewing all points, run `./ww feedback prune --dry-run --json`.
Pruning is separate from deduction and workflow completion. It deletes points
absent for at least five subsequently completed tasks; occurrence frequency
does not decide whether to propose a rule. Retain any stale point still useful
for a proposed or deferred rule using `--keep <point-id>` (repeatable), then
run `./ww feedback prune [--keep <point-id> ...] --json`. Do not delete points
by hand or use elapsed time as a new pruning criterion.

Report written rule IDs, scope, files changed, and pruned/retained candidate IDs.
