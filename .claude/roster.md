# ctx-yield — capability roster

Activated capabilities for this project and what each is used for. Shown at the
`/sia:build` pre-flight gate before any work begins.

| Capability | Type | Used for |
|---|---|---|
| `sia-module-builder` | agent (`.claude/agents/`) | Per-module build executor in agentic builds; holds the standing worker contract (own-files-only, coordination files, one commit per module) |
| pytest | dev tooling | Test suite; every module lands with its tests in the same commit. Runtime stays stdlib-only |
| `ANTHROPIC_API_KEY` | secret (env only) | The `--exact` count_tokens path exclusively; documented in `.env.example`, never committed |
| — no MCPs — | | Pure-stdlib local CLI; no external service integrations apply |
