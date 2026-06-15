# Handover: speed up Windows & zipapp CI

Goal: make pip's two slowest CI jobs (Windows tests, zipapp tests) run meaningfully
faster, keeping pip dogfooding, without piling on runners. This branch does that by
**sharding** each slow suite across runners. Your job: validate it on another machine.

## TL;DR of the change

- New, dependency-free test sharder in `tests/conftest.py`: `--num-test-groups N`
  `--test-group G` deselects every test whose `zlib.crc32(nodeid) % N != G-1`.
  Deterministic across xdist workers, so collections match; balanced by count.
- `.github/workflows/ci.yml`:
  - **Windows**: 3 balanced shards (was a crude `test_install` vs `not test_install`
    2-way split), `--dist worksteal`.
  - **zipapp**: 3 shards, moved from `macos-latest` to `ubuntu-latest` (the macos pool
    is capacity-limited and delayed shard *starts*; the suite is platform-agnostic),
    `--dist worksteal`, and the unnecessary `needs: [packaging]` gate dropped
    (a maintainer already removed that gate from the other test jobs in #13338).
- No `src/pip` changes. No new dependencies.

## Branches (pushed to `origin` = github.com/notatallshaw/pip)

- `ci-speedup-windows-zipapp` — **the deliverable** (sharding-only). Base the PR on this.
  This handover branch (`ci-speedup-handover`) = that branch + this doc.
- `pip-startup-lazy-imports` — **held, do not bundle here.** Lazy-imports rich/pygments/
  urllib in the pip CLI (extends startup issue #4768). It cuts `pip --version` import
  time ~29% (226->160ms) and is a genuine win *for pip users*, but a CI run proved it
  does **not** speed the test suites (tests are dominated by install/build work, not pip
  import). Ship it as its own startup PR later if desired.

## Results already measured (GitHub-hosted runners; per-JOB durations, which are
runner-spec bound and so comparable across repos/runs — the fork caps at 20 concurrent
jobs, which staggers *start* times but not durations)

| Suite (3 shards) | before | after (worst shard) | speedup |
|---|---|---|---|
| zipapp | 11.3 min (single macos job) | 6.5 min (ubuntu) | ~42% |
| Windows | 9.6 min (worst of 2 groups) | ~6.4 min median / 7.2 worst | ~33% median |

- Baseline run (pypa/pip main): `27504647980`.
- New-config run: `27515960125` (all green incl. the `check` gate). That run used an
  extra `-n 8` oversubscription that was later dropped as CI-neutral, so its numbers
  represent the shipped `-n auto` config (the difference was within runner noise).
  Re-run CI on this exact commit (step 3) for a fresh confirmation.

Note: this does **not** hit a literal 50% on Windows. That was a deliberate call —
50% on Windows would need ~5 shards, and the decision was to cap at 3 shards (fewer
jobs = fewer random network failures). zipapp could clear 50% with a 4th ubuntu shard
if wanted. Oversubscription (`-n 8`) was tried and dropped: ~14% locally but CI-neutral.

## How to validate on another machine

Prereqs: a clone of pip, Python 3.10-3.15, `nox`, and (for the CI part) `gh` + a fork
with Actions enabled.

```
git fetch origin ci-speedup-windows-zipapp
git switch ci-speedup-windows-zipapp
```

### 1. Sharder is a correct, balanced partition (fast, no venv needed)

```
# counts should sum to the unsharded total, be roughly equal, and not overlap
for g in 1 2 3; do
  python -m pytest tests/functional --collect-only -q -p no:cacheprovider -o addopts="" \
    --ignore=tests/functional/test_proxy.py --num-test-groups 3 --test-group $g | tail -1
done
# misuse should error clearly:
python -m pytest tests/unit/test_options.py --collect-only -q -o addopts="" --num-test-groups 3   # -> error: must be supplied together
```

### 2. A shard actually runs green (end-to-end through nox)

```
# builds sdist + installs, then runs shard 1 of 3 of unit+functional with worksteal
python -m nox -s test-3.12 -- tests/unit tests/functional \
  --num-test-groups 3 --test-group 1 -n auto --dist worksteal -q
# repeat for --test-group 2 and 3 if you want full coverage; together they equal a full run.
```

To validate the zipapp path specifically, add `--use-zipapp` and target `tests/functional`.

### 3. Confirm the CI speedup (the real proof)

On a fork with Actions enabled (Settings -> Actions -> "Allow all actions"):

```
gh workflow run CI --repo <you>/pip --ref ci-speedup-windows-zipapp
# wait, then compare per-job durations to a baseline run of main:
gh run view <run_id> --repo <you>/pip --json jobs \
  -q '.jobs[] | select(.name|test("Windows|zipapp")) | "\(.startedAt) \(.completedAt) \(.name)"'
```

Compare the worst Windows shard and worst zipapp shard against a `main` run's
`tests / .. / Windows / 2` and `tests / zipapp` jobs. Expect ~33%/~42% as above.

## Validation checklist

- [ ] Shards sum to the full collection, are balanced, and don't overlap (step 1).
- [ ] Each shard runs green via nox (step 2); the three shards together cover the suite.
- [ ] `--use-zipapp` shard runs green.
- [ ] CI run is green including the `check` job (step 3).
- [ ] Worst Windows shard and worst zipapp shard are ~30-45% faster than the main baseline.
- [ ] `git diff main -- src/pip` is empty (no production code changes on this branch).

(Scratch handover file — not part of the PR. Remove before opening the PR from
`ci-speedup-windows-zipapp`.)
