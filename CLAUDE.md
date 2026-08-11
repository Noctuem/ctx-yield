# ctx-yield — session handoff

<!-- sia-reconciled: #17 2026-08-11 -->

- **Spec:** `notes/projects/specs/implemented/ctx-yield.spec.md` in the SIA hub
  (`~\SIA`) — frozen since 0.1.0 shipped.
- **Where we are:** 0.1.0 shipped 2026-08-11 — full pipeline (corpus → tokens →
  recall → growth → cli), 62 tests, CI green on GitHub Actions, installable
  from the public repo.
- **Next:** 1) live `--exact` check against count_tokens + cache-hit rerun
  (needs `ANTHROPIC_API_KEY`, human-provided); 2) use the scan's never-recalled
  report to drive a real context prune; 3) PyPI publish so `pipx install
  ctx-yield` works by name as the README promises.
- **Guard (PUBLIC repo):** never commit the repo owner's personal name or
  absolute local paths; author identity is Noctuem. Grep staged changes before
  every push.
- **Pointers:** `TODO.md` (backlog), `Session_Log.md` (append-only session
  record).
