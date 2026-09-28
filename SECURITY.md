# Security Policy

## Supported versions

Security fixes are provided for the latest released version of ww. The project
is currently pre-1.0; users of a development checkout should update to the
latest commit before reporting a vulnerability.

| Version | Supported |
| --- | --- |
| Latest release | :white_check_mark: |
| Earlier releases | :x: |

## What ww can do on your machine

ww is a command runner, so the honest answer to "could this execute something
harmful?" is *yes, if your configuration tells it to* — and no scanner can say
otherwise about a tool of this kind. What follows is the trust boundary, so you
can check it rather than take it on faith. Every claim below is greppable in a
clone.

**`ww-agentic-workflows.yaml` is executable configuration.** A handler's `argv` runs
directly, and a `shell` handler runs through `/bin/sh -c`. Whoever writes or
edits that file has code execution on the machine running ww, exactly as with a
CI configuration. Review changes to it the same way. ww narrows the blast radius
where it can: shell arguments are passed as positional parameters rather than
spliced into the command string, and template values are rejected in shell
source for that reason — pass them through `args` or `env`.

**ww makes no network requests of its own, with one exception.** The update
check runs `git fetch` against the remote your checkout already points at, at
most once a day, and reports nothing anywhere. Turn it off with
`"update_check": false` in `ww-agentic-workflows.json` or `WW_UPDATE_CHECK=0`.
Anything else that reaches the network does so because a handler you configured
told it to.

**Extensions run arbitrary code in your interpreter.** They are Python entry
points, discovered from your environment. Installing one is trusting its author
as much as you trust ww itself.

**The operator page binds a local port.** An interactive `ui: true` step serves
a page on `127.0.0.1` for the duration of one wait, and only for that long.

**`.ww/` may hold secrets.** Task errors, artifacts, command output, and
extension settings live there, and any of them can contain values your workflow
was given. Treat the directory as private and sanitize before sharing. The audit
log `.ww/executions.jsonl` is owner-readable and deliberately records a stable
error code instead of arbitrary error text.

**Dependencies.** One runtime dependency, `PyYAML`, with no transitive
dependencies of its own. YAML is parsed with `safe_load` everywhere. CI audits
the installed set against known advisories on every run, and its GitHub Actions
are pinned to commit SHAs rather than movable tags.

## Reporting a vulnerability

Please do not report suspected vulnerabilities in a public issue or discussion.
Use [GitHub's private vulnerability reporting form](https://github.com/from-developers-for-developers/agentic-workflows/security/advisories/new)
for this repository instead.

Include the affected ww version or commit, supported platform and Python
version, a minimal reproduction, the impact you observed, and any proposed
mitigation. Do not include credentials, private `.ww/` data, or other sensitive
runtime files unless they are essential and have been sanitized.

Maintainers will acknowledge a report within seven days and use the private
report to coordinate investigation, remediation, and disclosure. If a report
is not accepted, maintainers will explain the decision through that private
channel.
