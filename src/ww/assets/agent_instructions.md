# Working with ww

ww saves progress, runs handlers and names the next role; it is only for
development.

If you were started with a `./ww instruction ... --role worker` command,
or a ww page assigned you work, you are a ww worker: run that command
first and follow the page it prints, which says everything you need.
A worker never runs `./ww discover`, never starts or records a task, and
needs nothing else from this file. Never run or bypass a ww handler,
edit ww state, or read `ww.yaml` or ww's source to work out the next step.

Otherwise, run `./ww discover` only at the moment you are about to change
files for a request, before the first change, once per session. It says
whether ww is used here and how to start or record the work. Do not run it
for questions, reviews, explanations or investigations, which never use ww;
not to look around or check status; and not again once you know the answer.
To continue a task, run `./ww instruction <task-id> --role manager` instead.

For `repeat_until` assessments, follow the page's session and exact-message
protocol. In auto, keep the assessor alive; delegate outcome agent work to a
separate worker. If nested delegation is unavailable, the manager relays the
unchanged findings and result between that worker and the same assessor. An
already launched outcome worker performs its own assignment, without
delegating that assignment again. WW continues to own all handlers.

Judged-rule verification follows the same retained-session and exact-message
protocol. Keep the verifier alive while a separate worker fixes its findings;
if it cannot delegate, the manager relays the exact findings and repair result
back to that verifier. Follow `repeat_communication` to locate each round's
evidence. Verifiers submit structured `--rule-result` verdicts, not assessment
outcomes; the original completion stays held until the selected rules pass.
Repair only the reported violations using the repair assignment's settings
and lifecycle; do not repeat the source action or its hooks. A repair worker
performs its own assignment and may dispute a reported rule with evidence.

A regular step may attach `assess: {assess: <prompt>, ...}`. Perform its work
first, then assess in the same session and record the selected outcome through
WW. For attached `repeat_until`, reassess after exact repair results without
replaying the original action; completion hooks wait for the terminal outcome.
Goto branches declare their own permissions. The inner `question` spelling is
deprecated; use `assess` and see `ww docs deprecations` for migration guidance.
