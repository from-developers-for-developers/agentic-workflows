# Releases and development builds

Releases are built from `main`; `dev` is never published. Every push to
`main` runs `.github/workflows/release.yml`: it calls the release gates
(Python compatibility, formatting, lint, types, tests, packaging and
dependency audit), chooses the next version, builds it, publishes it to PyPI
as `ww-agentic-workflows`, and tags the released commit `v<version>`. A push
that changes nothing under `src/`, `ext/` or `pyproject.toml`, such as a
documentation fix, releases nothing: the run ends with the notice "Nothing to
release". Pushes to `dev` run the same gates through
`.github/workflows/dev-ci.yml` and publish nothing.

## Branches

- A bug found in `main`, the released code, is fixed on a branch from `main`
  and merged into `main` (the `hotfix` workflow, then `merge-to-main`).
- A bug found in `dev` is fixed on a branch from `dev` and merged into `dev`
  (the `bugfix` workflow, then `merge-to-dev`).
- New functionality is built on a branch from `dev` and merged into `dev`
  (`task`, `express` or `roadmap`, then `merge-to-dev`).
- `dev` is merged into `main` by hand when its changes should be released.

## Versioning

Nobody edits the version for a release: CI numbers it.
`scripts/release_version.py` reads the version in `pyproject.toml` as the
release series. While PyPI lacks that exact version it is released as
written; otherwise the highest PyPI version of the series is incremented:

| Series in `pyproject.toml` | On PyPI | Released |
| --- | --- | --- |
| `1.0.0b1` | `1.0.0b1` … `1.0.0b4` | `1.0.0b5` |
| `1.0.0rc1` | no RC yet | `1.0.0rc1` |
| `1.0.0` | `1.0.0`, `1.0.1` | `1.0.2` |

The number is written only into the build's checkout and never committed, so
`main` and `dev` never disagree about it; the release tag records it. A yanked
version stays taken. ww is in public beta: releases are `1.0.0b1`, `1.0.0b2`,
and so on. Changing series is the one manual edit: set `pyproject.toml`'s
version to `1.0.0rc1` for release candidates, `1.0.0` for the stable release,
or `1.1.0` for the next minor line, on `dev`, and the next release follows it.
pip and pipx install a beta when no stable release exists, so
`pipx install ww-agentic-workflows` installs the newest beta today. Earlier
`1.0.0.devN` snapshots, published from `dev` before the first beta, remain on
PyPI and sort below `1.0.0b1`.

The workflow checks metadata, bundled resources and license files, and
installs the final wheel in an isolated environment before uploading the exact
same wheel and source distribution. If a publish or tag job fails, rerun
that failed job: it reuses the built files and the chosen version. Rerunning
the whole workflow after a successful upload chooses the next version and
releases the same commit again.

## Making a release

Merge `dev` into `main` and push `main`. A fix for the released code is
merged into `main` through `hotfix` and `merge-to-main`; pushing `main`
releases it as the next number.

## Running the development version

`dev` is used from a clone, installed in editable mode:

```console
git clone https://github.com/from-developers-for-developers/agentic-workflows.git ~/tools/agentic-workflows
git -C ~/tools/agentic-workflows checkout dev
pipx install --editable ~/tools/agentic-workflows
```

The package name is the PyPI one, so remove a PyPI install first with
`pipx uninstall ww-agentic-workflows`, or keep both with a suffix:
`pipx install --editable --suffix=-dev ~/tools/agentic-workflows` installs the
clone as `ww-agentic-workflows-dev`, which a project selects with
`"executable": "ww-agentic-workflows-dev"` in its `ww.json` or local settings
file; see [Choosing the ww binary](features.md#choosing-the-ww-binary).

`ww-agentic-workflows upgrade` fast-forwards an editable checkout's branch;
update notices compare the checkout with its remote. A PyPI install upgrades
to the newest release instead, betas included while the installed version is
a beta.

`upgrade` does not wait for open tasks. A version that changes the task state
format can leave tasks started by an older one unreadable, so read
[CHANGELOG.md](../CHANGELOG.md) before upgrading a project with work in
progress.

## One-time publishing setup

1. In the GitHub repository, create the environment `pypi`. Restrict its
   deployment branches to `main`. Add yourself as a required reviewer if each
   release should wait for approval; leave it unset to publish automatically.
2. In PyPI, add a GitHub Trusted Publisher to `ww-agentic-workflows`
   (Manage project → Publishing):
   - Owner: `from-developers-for-developers`
   - Repository: `agentic-workflows`
   - Workflow filename: `release.yml`
   - Environment: `pypi`
3. Remove the old publisher for `dev-publish.yml` and `pypi-dev`, and delete
   the `pypi-dev` environment in GitHub.
4. Push `main`. If the setup was incomplete, finish it and rerun the failed
   job.

The tag job pushes `v<version>` tags with the workflow's own token, so a tag
protection rule or ruleset on `v*` must let GitHub Actions create them.

No PyPI password or API-token secret is required. Only the publish job has
`id-token: write`; it downloads the checked artifacts without checking out or
executing project code.

See PyPI's documentation for
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
