# ctx-yield

ctx-yield is a lightweight, local-first CLI that measures whether your AI
context system is earning its tokens. It inventories the context files your
coding agent force-loads (CLAUDE.md and its imports, skills, commands, agents,
memory, AGENTS.md), weighs them with correct token counts, joins them against
your actual session transcripts to show which files were recalled and which
were never read at all, tracks their growth across git history, and can gate
your CI on a token budget. For structural linting use
[agnix](https://github.com/agent-sh/agnix); for spend use
[ccusage](https://github.com/ryoppippi/ccusage); for stale references use
[ctxlint](https://github.com/YawLabs/ctxlint) or
[stalebrain](https://github.com/stalebrainlabs/stalebrain) — ctx-yield measures
whether your context is earning its tokens. Note what it does *not* claim:
context size has not been shown to drive instruction compliance
([arXiv 2605.10039](https://arxiv.org/abs/2605.10039)) — ctx-yield optimizes
cost and headroom, not obedience.

## Quick start

v1 is in build — the CLI below is the target interface, not yet published.

```sh
pipx install ctx-yield          # once published
ctx-yield scan [path]           # ranked report: never-recalled first, then weight
ctx-yield scan --exact          # exact token counts via the count_tokens API (needs ANTHROPIC_API_KEY)
ctx-yield scan --budget 15000   # non-zero exit when force-load exceeds the budget (CI-gateable)
```
