---
name: ww
description: Carry out a development request through ww, this project's workflow tool. Use only when the request changes something (implement, fix, refactor, merge, edit documentation) or the user explicitly asks to work via ww, to start or continue a ww task, or to run a ww workflow; where `./ww discover` says ww is used by default, use it before changing files for any request, since every change then goes through ww. Never use it for reviews, questions, explanations, investigations, status checks or other read-only work, even when they concern ww or a ww task.
---

# Work through ww

ww is for development: requests that change files. A review, question,
explanation, investigation, status check or other read-only request never
goes through ww; answer it directly, without these steps and without a task,
even where ww is used by default. When a read-only conversation turns into a
change, start from step 1 then.

1. Run `./ww discover` and read all of it. If it says ww is disabled, stop:
   do not use ww, and tell the user. If it says ww is used only on request
   and the user did not explicitly ask for ww (invoking this skill counts),
   carry out the request without ww and do not ask.
2. To continue an existing task, run
   `./ww instruction <task-id> --role manager` instead of starting a new one.
3. Otherwise decide how the request is carried out, in this order:
   a. It names a ticket or clearly matches a configured workflow's
      description, and is more than a small change: propose that workflow, and
      offer the alternative in the same choice, "run `<workflow>`" / "just do
      it, register afterwards". A workflow the user named is started without
      asking. When one is chosen, keep its default
      modes unless the user's wording matches another mode, and start the task
      with the start command `discover` shows. Pass the user's requirements,
      normalized, as `--requirements`. When the request names an external
      ticket, such as a Jira key, use that key as the task ID; omit the ID
      only when there is none or the workflow obtains its own.
   b. Otherwise it is direct work: do it as you would in a plain conversation,
      with no question asked, then run
      `./ww record <task> --summary "..."`. `./ww lookup [<task>] --agent
      <agent>` names the task and the command; pass the task the conversation
      works on or the request names, as written. Read-only work needs no task.
   c. Judge each prompt on its own. A series of small requests stays a series
      of direct-work entries; never promote them into a workflow because they
      add up.
   d. Ask only when it is genuinely ambiguous, and never again on the same
      task once the operator chose direct work.
   e. If `record` fails or is forgotten, ww records the commits it had not
      seen on the next run; say so, and do not retry it endlessly.
   When the first page of `start` says rules have no check
   yet, tell the user once and carry on: it never blocks the task, and the
   `ww-scriptize` skill builds those checks when they want it. When it says
   ww collects debug info or workflow feedback, tell the user once that it
   is collected at the end of the run and stays on this machine, then never
   mention it again: ww asks for it at the end of the run, and only the user
   can send it.
4. Follow every ww response exactly: run each displayed command with all
   placeholders replaced, and keep going until ww reports that the workflow
   is complete or reports an error. On `interactive: true` steps, converse
   naturally until intent to finish is clear, ask if ambiguous, and treat
   "done for today" as a pause. Record and end the interaction before
   completing it. Use the host choice tool when available; keep asynchronous
   choices pending until an answer arrives, and do not treat timeouts or
   preselection as answers. After completion, relay any optional
   `ww-deduce-feedback` suggestion; do not insert learning into the plan.
   Deduction uses ww commands and explicit existing point IDs; rule review
   and pruning are separate, and rules require operator approval.
5. Never run a ww-owned handler yourself, edit ww state, or read
   `ww.yaml` or ww's source to work out what to do next. For a handler repair
   assignment, fix the cause and complete with an artifact; ww retries the
   command. In `auto`, the manager dispatches the repair with `next`. When
   ww reports `awaiting_operator` (a failed handler or work, an interrupted
   command, a changed workflow), stop and report the task, its
   `operator_reason` and the exact error to the user (unless they gave a
   standing authorization for routine repairs such as dependency installation,
   formatting or retries: apply it to a stop of that kind without asking again,
   and ask only for a material decision or an action outside it); when they
   decide, run the recovery command ww showed, `./ww next <task-id> --retry` to run the
   handler again, `--force --reason` to skip it, or `--replan` / `--keep-plan`
   for a changed workflow. Missing item bookkeeping is not such a stop: an
   items step's page lists the unresolved, unreported, or missing-field items
   with their commands; record them and run its completion command again. A
   failed identity request (`REQUEST-…`) is only retried with `--retry` or dropped
   with `./ww reset <request-id> --yes`, both shown on its page; `--force`
   is refused there too.
