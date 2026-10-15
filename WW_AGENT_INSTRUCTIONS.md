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
