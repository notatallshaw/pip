# pip Release Manager Checklist

## Pre-Release Preparation
- [x] Ensure latest `nox` is installed
- [x] Verify `main` branch is in releasable state
- [x] Confirm release date within scheduled month (January, April, July, October)
- [x] Check if there are changes since last release (skip if none)
- [x] Decide if pre-release period is needed

## Standard Release (YY.N)
1. [x] Create new branch `release/YY.N` off `main`: `git checkout -b release/YY.N`
2. [x] Run `nox -s prepare-release -- YY.N` which will:
   - Check no files are staged (errors if any)
   - Update and commit `AUTHORS.txt` if needed
   - Generate `NEWS.rst` using towncrier
   - **PAUSE** for manual review and staging of `NEWS.rst`
   - Commit version bump for release
   - Create git tag `YY.N`
   - Commit version bump for development
3. [x] Validate docs build: `nox -s docs`
4. [x] Submit PR for `release/YY.N` branch to `main`
5. [ ] Wait for CI to pass
6. [ ] Merge PR into `main`
7. [ ] Pull merged changes locally: `git pull`
8. [ ] Push the tag: `git push upstream YY.N`
9. [ ] Go to https://github.com/pypa/pip/actions
10. [ ] Find latest "Publish Python 🐍 distribution 📦 to PyPI" workflow run
11. [ ] Wait for build step to complete
12. [ ] Approve PyPI environment to trigger publishing
13. [ ] Regenerate `get-pip.py` in [get-pip repository](https://github.com/pypa/get-pip)
14. [ ] Commit regenerated `get-pip.py` changes
15. [ ] Submit PR to [CPython](https://github.com/python/cpython):
    - Add new pip version to `Lib/ensurepip/_bundled`
    - Remove existing version
    - Update versions in `Lib/ensurepip/__init__.py`

## Additional Steps (If Python Version Support Dropped)
- [ ] Publish new `M.m/get-pip.py` for obsolete Python version
- [ ] Update `all` task in `tasks/generate.py` (get-pip repository)
- [ ] Submit PR to [psf-salt repository](https://github.com/python/psf-salt) adding new `get-pip.py` to `salt/pypa/bootstrap/init.sls`

## Additional Steps (If get-pip.py Template Changed)
- [ ] Duplicate `templates/default.py` as `templates/pre-YY.N.py` before updating
- [ ] Update `tasks/generate.py` to specify `M.m/get-pip.py` uses `templates/pre-YY.N.py`

## Bugfix Release (YY.N.Z+1) - When Not Including All Main Changes
1. [ ] Create branch `release/YY.N.Z+1` off tag `YY.N`: `git checkout -b release/YY.N.Z+1 YY.N`
2. [ ] Cherry-pick fixed commits from `main`, resolving conflicts
3. [ ] Run `nox -s prepare-release -- YY.N.Z+1` (same process as standard release)
4. [ ] Review and stage `NEWS.rst` when prompted
5. [ ] Merge `main` into release branch: `git merge main`
6. [ ] Remove news files already included in this bugfix release
7. [ ] Push branch to GitHub: `git push origin release/YY.N.Z+1`
8. [ ] Submit PR against `main`
9. [ ] Wait for CI to pass
10. [ ] Merge PR into `main`
11. [ ] Continue with standard release from step 7 (pull changes, push tag, etc.)

## Bugfix Release (YY.N.Z+1) - Including All Main Changes
- [ ] Follow standard release process with bugfix version number

---

**Note:** For detailed information, see [docs/html/development/release-process.rst](docs/html/development/release-process.rst)
