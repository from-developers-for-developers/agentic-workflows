# Deprecations

This file lists supported syntax scheduled for removal. WW reports a warning
when an applied configuration uses deprecated syntax. No removal date is set
unless the entry states one.

## Assessment prompt key `question`

Deprecated on 2026-10-16. Use `assess` inside the full assessment mapping.
`question` remains a backward-compatible alias. Supplying both names is an
error. The compact `assess: <prompt>` form remains supported.

Before:

```yaml
- assess:
    question: Are there issues?
    positive:
      handler: fix-findings
```

After:

```yaml
- assess:
    assess: Are there issues?
    positive:
      handler: fix-findings
```

The same alias is accepted with a warning inside an assessment attached to a
regular step. For the explicit standalone `name: assess` form, rename its
`question` field to `assess` too. Imported and inherited configuration is
included in the warnings; source paths identify the field to change.
