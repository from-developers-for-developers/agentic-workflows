## Codex execution boundary

Run WW CLI commands using Codex's normal sandbox policy. Do not request
escalation preemptively.

If a specific WW command fails because sandbox access prevents it from
completing, rerun only that command outside the sandbox with approval. WW
remains the trusted execution boundary for its handlers; do not invoke its Git
or other handlers separately.

@WW_AGENT_INSTRUCTIONS.md
