# Releasing FAFO

## Version policy

Semantic Versioning. While the major version is 0, any release may change behavior; the CHANGELOG
says so. The version lives in one place, `src/fafo/__init__.py` (`__version__`); `pyproject.toml`
reads it. Every release needs a `## [X.Y.Z]` section in `CHANGELOG.md`.

## How to tag

1. Move the `Unreleased` entries into a new `## [X.Y.Z]` section and set `__version__` to `X.Y.Z`.
2. Merge that to `main` with CI green. `tests/test_release_consistency.py` checks that the version
   and the CHANGELOG line up.
3. Tag the merged commit and push the tag: `git tag -s vX.Y.Z && git push origin vX.Y.Z`.

The first tag (v0.1.0) waits until the independent security review is closed.

## What the workflow does

`.github/workflows/release.yml` runs on a push of a `v*` tag, and by hand (`workflow_dispatch`).
A run on a branch only builds and checks.

- `package` calls the shared `python-package-release.yml` (same pin as `ci.yml`). It builds the
  sdist and wheel, runs `twine check --strict`, refuses a tag that disagrees with the built version,
  checks that `CHANGELOG.md` has a section for the version, installs the wheel in a fresh venv and
  runs `fafo --version`. On a tag it attaches the files, SBOMs, `SHA256SUMS` and build provenance
  to the GitHub release, using the CHANGELOG section as the notes. A manual run publishes only on
  a tag ref with the `publish` input set.
- `pypi` runs only when the repository variable `PYPI_PUBLISH` is the string `true` and the ref is
  a version tag. It downloads the `dist` artifact `package` built and publishes it with
  `pypa/gh-action-pypi-publish` as a direct step, in the `pypi` environment, with an OIDC token
  and no stored credential. With the variable unset nothing is sent to PyPI; a tag still creates
  the GitHub release.

## One-time PyPI setup (owner)

On PyPI, add a trusted publisher for the project `fafo` (a pending publisher before the first
upload):

- Owner and repository: the GitHub owner and name of this repository.
- Workflow file name: `release.yml`
- Environment name: `pypi`

On GitHub, create the environment `pypi` (add required reviewers if you want a manual gate), then
set the repository variable `PYPI_PUBLISH` to `true` when you are ready to publish. Unset it or
set any other value to turn publishing off.

## Clean-install check

After a tag build, or locally before tagging, check that the wheel installs and runs from nothing:

```
python -m pip install build
python -m build
scripts/clean_install_check.sh dist/fafo-X.Y.Z-py3-none-any.whl
```

The script makes a fresh venv in a directory under `~/.cache` (never `/tmp`), installs the wheel,
runs `fafo --version`, `fafo doctor` and `fafo table` with a throwaway state directory, and removes
only that directory.
