# ctx-yield

ctx-yield is a lightweight, local-first CLI that measures whether your AI
context system is earning its tokens. It inventories the context files your
coding agent force-loads (`CLAUDE.md` and its imports, skills, commands,
agents, memory, `AGENTS.md`), weighs them with correct token counts, joins
them against your actual session transcripts to show which files were
recalled and which were never read at all, tracks their growth across git
history, and can gate your CI on a token budget.

**For structural linting use [agnix](https://github.com/agent-sh/agnix); for
spend use [ccusage](https://github.com/ryoppippi/ccusage); ctx-yield measures
whether your context is earning its tokens.** For stale references also see
[ctxlint](https://github.com/YawLabs/ctxlint) and
[stalebrain](https://github.com/stalebrainlabs/stalebrain) (GitHub
search-level links — treat as related projects to look up, not verified
exact repos).

Note what ctx-yield does *not* claim: a 1,650-session study
([arXiv 2605.10039](https://arxiv.org/abs/2605.10039)) found that context
size does **not** drive instruction compliance. ctx-yield optimizes **cost
and headroom**, not obedience — a smaller, cheaper context is not evidence
your agent will follow it any better.

## Quick start

```sh
pipx install ctx-yield
ctx-yield scan                  # scan the current directory
ctx-yield scan [path]           # ranked report: never-recalled first, then weight
ctx-yield scan --exact          # exact token counts via the count_tokens API (needs ANTHROPIC_API_KEY)
ctx-yield scan --budget 15000   # non-zero exit when force-load exceeds the budget (CI-gateable)
```

Everything runs offline by default: file discovery, token weighing (a
calibrated per-model heuristic), transcript joins, and git-history growth are
all local, stdlib-only, and read-only — ctx-yield never edits your context
files and never phones home.

## What a scan shows

```
$ ctx-yield scan
ctx-yield scan: /path/to/your-project

path                                             kind                 tokens  recalls last recalled               growth
------------------------------------------------------------------------------------------------------------------------
* .claude/skills/onboarding/SKILL.md             skill         4430 (+/-15%)        0 never                            !
* docs/legacy-notes.md                           import        1331 (+/-15%)        0 never
  CLAUDE.md                                      claude-md      612 (+/-15%)       14 2026-08-10T09:12:00Z             !
(* = never recalled)

Total force-load tokens: 6373
Never-recalled files: 2/3
Force-load grew 3.1x in 90 days
```

Rows are ranked **never-recalled files first**, then by token weight
descending within each group — the files most worth pruning float to the
top. `!` in the growth column flags a file (or, on the summary line, the
whole corpus) that grew more than 2x over the trailing 90-day window.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `path` (positional) | current directory | project root to scan |
| `--budget N` | none | exit 1 when total force-load token weight exceeds `N` (else 0) |
| `--sessions K` | `30` | how many recent session transcripts to join against |
| `--exact` | off | use Anthropic's `count_tokens` API instead of the offline heuristic (needs `ANTHROPIC_API_KEY`) |
| `--model MODEL` | `claude-opus-5` | tokenizer target model for both heuristic calibration and `--exact` |
| `--json` | off | print one machine-readable JSON document instead of the text report |

Exit codes: `0` clean (or budget not exceeded), `1` budget exceeded. A
missing git history or missing session transcripts are not errors — those
sections degrade gracefully and the scan still completes.

## `--exact` and `ANTHROPIC_API_KEY`

`--exact` is entirely optional. Without it, ctx-yield uses a calibrated
offline heuristic (chars-per-token, tuned per tokenizer family) and reports
a `±` error bar alongside every count — no key, no network call, ever.

With `--exact`, ctx-yield calls Anthropic's free `/v1/messages/count_tokens`
endpoint (no Anthropic SDK dependency — plain `urllib.request`) and caches
results by content hash so repeat scans never re-count unchanged files. Set
`ANTHROPIC_API_KEY` in your environment (see `.env.example`) to enable it —
if it's unset, or the call fails for any reason, ctx-yield falls back to the
heuristic automatically and tells you why.

## CI gate

Fail a build when your context system's force-load weight exceeds a budget:

```yaml
# .github/workflows/ctx-yield.yml
name: ctx-yield
on: [pull_request]
jobs:
  budget:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pipx install ctx-yield
      - run: ctx-yield scan --budget 20000
```

A non-zero exit (budget exceeded) fails the job.

## What ctx-yield is not

No dashboards, no UI, no telemetry/SaaS component, no `--fix` or mutation of
your context files, no multi-vendor breadth (this targets Claude Code's
context conventions specifically), and no runtime interception of your
agent. It reads what's on disk and in your own transcripts, and reports.
