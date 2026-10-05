# Development releases

Every push to `dev` runs `.github/workflows/dev-publish.yml`. It calls the
existing release gates (Python compatibility, formatting, lint, types, tests,
packaging and dependency audit), then builds and publishes a development
snapshot of `ww-agentic-workflows` to PyPI. Other branches and pull requests
cannot publish through this workflow.

## Versioning

The next planned release is `1.0.0`. Snapshots use `1.0.0.devN`, where `N` is
GitHub's run number for this workflow. Failed runs can leave gaps. Reruns keep
the same version and skip files already present on PyPI, allowing an interrupted
upload to finish. Keep this workflow's filename and run-number sequence when
changing it; resetting the sequence can collide with published versions.

The version is stamped only in the build job's checkout. It does not change
the committed version, create a Git tag, or create a GitHub release. Commit
messages do not control snapshot versions. When the next planned release
changes, update `--base-version` in the workflow.

The workflow checks metadata, bundled resources and license files, and installs
the final wheel in an isolated environment before uploading the exact same
wheel and source distribution. Passing CI does not make a snapshot a stable
release.

Install the latest development version with:

```console
pip install --upgrade --pre ww-agentic-workflows
```

Or select a particular snapshot with `ww-agentic-workflows==1.0.0.dev42`.

Once installed, `ww-agentic-workflows upgrade` automatically allows newer
prereleases when the installed version is a development build, beta or RC.
Normal commands show a cached update notice when PyPI has a newer compatible
version; `ww-agentic-workflows updates --now` forces a fresh check.

## One-time publishing setup

1. In the GitHub repository, create the environment `pypi-dev`. Restrict its
   deployment branches to `dev`. Leave required reviewers unset for automatic
   publishing.
2. In PyPI, configure a GitHub Trusted Publisher for `ww-agentic-workflows`:
   - Owner: `from-developers-for-developers`
   - Repository: `agentic-workflows`
   - Workflow filename: `dev-publish.yml`
   - Environment: `pypi-dev`
3. If the package does not exist yet, configure a pending publisher through
   your PyPI account's Publishing page with those same values. The first
   successful upload creates the project.
4. Merge the workflow into `dev`. That push starts the first release. If the
   publishing setup was incomplete, finish setup and rerun the failed job.

No PyPI password or API-token secret is required. Only the publish job has
`id-token: write`; it downloads the checked artifacts without checking out or
executing project code.

See PyPI's documentation for
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
and [creating a project with a pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
