## What this changes

<!-- The behaviour that differs afterwards, in a sentence or two. -->

## Why

<!-- The problem it solves. Link the issue if there is one. -->

## How it was verified

<!-- The checks you ran, and anything you exercised by hand. -->

- [ ] `.venv/bin/python -m pytest`
- [ ] `.venv/bin/python -m ruff check src tests scripts` and `ext/ww/git`
- [ ] `.venv/bin/python -m mypy`
- [ ] `.venv/bin/python scripts/check_release_gates.py` (release-facing changes)
- [ ] `.venv/bin/python scripts/check_distribution.py` (release-facing changes)

## Checklist

- [ ] `CHANGELOG.md` updated under today's date, if the change is user-visible.
- [ ] Documentation under `documentation/` updated, if behaviour or `ww.yaml` changed.
- [ ] Persisted-format changes carry a schema bump and are called out as incompatible.
