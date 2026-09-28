---
name: ww
description: Carry out the user's request through ww, this project's workflow tool. Use when the user asks to work via ww, to start or continue a ww task, or to run a ww workflow, and before changing files for any request, since every change goes through ww. Not for questions or other read-only work.
---

# Work through ww

1. Run `./ww discover` and read all of it. If it says ww is disabled, stop:
   do not use ww, and tell the user.
2. To continue an existing task, run
   `./ww instruction <task-id> --role manager` instead of starting a new one.
3. Otherwise choose the workflow that matches the request, keep its default
   modes unless the user's wording matches another mode, and start the task
   with the start command `discover` shows. Pass the user's requirements,
   normalized, as `--init-artifact`. When the request names an external
   ticket, such as a Jira key, use that key as the task ID; omit the ID only
   when there is none or the workflow obtains its own.
   When no workflow fits and you are about to change files, do not start
   `catchall` directly: run `./ww lookup [<task>] --agent <agent>` with the
   task the conversation works on or the request names, as written, and
   follow its answer, then do the work as you would without ww. Questions and
   other read-only work need no task.
4. Follow every ww response exactly: run each displayed command with all
   placeholders replaced, and keep going until ww reports that the workflow
   is complete or reports an error.
5. Never run a ww-owned handler yourself, edit ww state, or read
   `ww-agentic-workflows.yaml` or ww's source to work out what to do next. On a handler
   failure, stop and report the task and the exact error to the user; when
   they decide, run the recovery command ww showed, `./ww next <task-id>
   --retry` to run the handler again or `--force --force-reason` to skip it.
   A loop at its iteration limit is escalated the same way, and the force
   leaves the loop.
