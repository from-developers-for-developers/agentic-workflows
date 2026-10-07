---
name: ww-debug-report
description: Report the debug records ww collected about its own behaviour to ww's GitHub issues, one issue per record, each shown in full and confirmed by the operator first. Use when `./ww discover` offers it and the operator says yes, or when the operator asks to report ww debug info.
---

# Report ww's debug records

ww's `debug.collect` mode keeps a local record per workflow run of how ww
itself behaved: errors, bugs and blockers first, then inconveniences, then
the events ww observed on its own (a `fail`, a forced `next`, a dispute,
every stop for the operator) and the operator's notes. Nothing is sent by
itself. Reporting is the operator's explicit decision, record by record, and
ww does the sending, not you.

When the operator tells you something ww did wrong that no record holds yet,
keep it with `./ww debug note <record-id|task-id|request-id> --summary "…"
[--detail "…"]`; it lands on the record, or waits for the run's record.

1. Run `./ww debug list`. If it shows no unreported record, say so and stop.
2. For each unreported record, run `./ww debug show <record-id>` and show the
   operator what it contains: that exact content becomes the issue body, on
   the public repository `from-developers-for-developers/agentic-workflows`.
   Point out anything that looks private (paths, names, requirements) so they
   can decline it.
3. Ask the operator, per record, whether to send it, with the host choice
   tool when available. Keep the choice pending until an answer arrives; a
   timeout or a dismissal is not a yes.
4. For each record they approved, run
   `./ww debug report <record-id> --yes`. With an authenticated `gh` CLI, ww
   creates the issue and prints its URL. Otherwise ww opens the browser on
   the repository's new-issue page with the title and body prefilled; tell
   the operator to review and submit it there, logged in. ww stores no
   credentials and reads none.
5. Relay the result: the issue URL, or that the browser page was opened.
   Records the operator declined stay under `.ww/debug/`, unreported, and
   ww offers them again on a later `discover` while `debug.report` is on.

Never pass `--yes` for a record the operator has not approved, never edit the
records under `.ww/debug/` yourself, and never publish their content through
another channel.
