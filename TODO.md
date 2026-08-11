# TODO — ctx-yield

## High

- [ ] Live `--exact` verification: one real count_tokens call matches, cache
  hit on rerun (needs `ANTHROPIC_API_KEY` in env — human-provided)
- [ ] Act on the first real scan: 9/18 context files flagged never-recalled on
  the maintainer's own corpus — drive an actual prune decision from the report

## Medium

- [ ] Publish to PyPI so `pipx install ctx-yield` resolves by name (README
  quick-start currently assumes it; installs from the git URL work today)

## Low

## Shelved

## Fleeting Ideas

## Done

- [x] Build v1 per spec module graph (corpus → tokens → recall → growth → cli)
  — shipped 0.1.0, 2026-08-11: 5 modules, 62 tests, CI green, fresh-install
  verified from the public repo
