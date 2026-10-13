# Releases and development builds

Releases are built from `main`; `dev` is never published. Every push to
`main` runs `.github/workflows/release.yml`: it calls the release gates
(Python compatibility, formatting, lint, types, tests, packaging and
dependency audit), then builds the version `pyproject.toml` names and
publishes it to PyPI as `ww-agentic-workflows`. A version already on PyPI is
not published again: the run ends with the notice "Nothing to publish", so a
push to `main` that does not change the version releases nothing. Pushes to
`dev` run the same gates through `.github/workflows/dev-ci.yml` and publish
nothing.

## Versioning

The version in `pyproject.toml` is the release. ww is in public beta:
releases are `1.0.0b1`, `1.0.0b2`, and so on, then `1.0.0` once it is stable.
pip and pipx install a beta when no stable release exists, so
`pipx install ww-agentic-workflows` installs the newest beta today. Earlier
`1.0.0.devN` snapshots, published from `dev` before the first beta, remain on
PyPI and sort below `1.0.0b1`.

The workflow checks metadata, bundled resources and license files, and
installs the final wheel in an isolated environment before uploading the exact
same wheel and source distribution. A rerun keeps the version and skips files
already on PyPI, so an interrupted upload can finish.

## Making a release

1. On `dev`, set the next version in `pyproject.toml` and the fallback
   `__version__` in `src/ww/__init__.py`, and give the changes a heading in
   `CHANGELOG.md`.
2. Merge `dev` into `main` and push `main`. The release workflow publishes the
   new version once the gates pass.

A fix the current release needs goes to `main` first, through the `hotfix`
workflow, with the next beta number (`1.0.0b2` after `1.0.0b1`); pushing
`main` publishes it. Merge `main` back into `dev` afterwards, so the fix and
the version are not lost by the next release.

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

No PyPI password or API-token secret is required. Only the publish job has
`id-token: write`; it downloads the checked artifacts without checking out or
executing project code.

See PyPI's documentation for
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
