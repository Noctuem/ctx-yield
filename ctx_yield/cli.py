"""ctx-yield CLI: does your AI context system earn its tokens?

Wires the pipeline together: :func:`ctx_yield.corpus.inventory` discovers the
force-loaded context files, :func:`ctx_yield.tokens.weigh` prices each one in
tokens, :func:`ctx_yield.recall.join` says whether it was ever actually read
back out of a real session transcript, and :func:`ctx_yield.growth.growth`
says whether it's getting heavier over time. This module's only job is to
join those four outputs into one ranked report and a budget-gate exit code.

Entry point: ``ctx-yield scan [path]`` (installed via ``[project.scripts]``).
See ``interface.md`` (repo root, gitignored coordination doc) for the
realized flag/exit-code/JSON-schema contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .corpus import inventory as corpus_inventory
from .growth import growth as growth_report
from .recall import join as recall_join
from .tokens import DEFAULT_MODEL, weigh

#: Default number of recent session transcripts the recall join scans.
DEFAULT_SESSIONS = 30

#: Default trailing window (days) the growth flag looks back over — kept in
#: sync with ctx_yield.growth.DEFAULT_WINDOW_DAYS.
DEFAULT_GROWTH_DAYS = 90

#: Default growth ratio that trips the "flagged" bit.
DEFAULT_GROWTH_THRESHOLD = 2.0

#: Cache subdirectory name, resolved against the scanned project's root.
CACHE_DIRNAME = ".ctx-yield-cache"


def _default_user_home() -> Path:
    """The real ``~`` for user-level corpus discovery. A thin wrapper around
    ``Path.home()`` so tests can monkeypatch this one function instead of
    the real home directory ever being touched by the test suite."""
    return Path.home()


def _default_projects_dir() -> Path:
    """The real Claude Code transcript root (``~/.claude/projects``). Same
    monkeypatch-seam reasoning as :func:`_default_user_home`."""
    return _default_user_home() / ".claude" / "projects"


def build_report(
    root: str | Path,
    *,
    budget: Optional[int] = None,
    sessions: int = DEFAULT_SESSIONS,
    exact: bool = False,
    model: str = DEFAULT_MODEL,
    user_home: Optional[str | Path] = None,
    projects_dir: Optional[str | Path] = None,
    now: Optional[datetime] = None,
    days: int = DEFAULT_GROWTH_DAYS,
    threshold: float = DEFAULT_GROWTH_THRESHOLD,
    cache_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Build the full scan report for ``root``.

    This is the testable core: every source of real-world/network/home-dir
    side effects (``user_home``, ``projects_dir``, ``now``, ``cache_dir``) is
    an explicit, injectable parameter with an inert default (``None`` /
    "skip"), matching the convention already established by ``corpus`` and
    ``recall``. :func:`main` is the thin CLI wrapper that supplies the real
    defaults.

    Returns a JSON-safe dict: ``{"meta", "records", "totals", "growth"}``.
    See ``interface.md`` for the full field-by-field shape.
    """
    root = Path(root).resolve()
    now = now if now is not None else datetime.now(timezone.utc)
    resolved_cache_dir = Path(cache_dir) if cache_dir is not None else root / CACHE_DIRNAME

    entries = corpus_inventory(root, user_home=user_home)
    recall_stats = recall_join(
        entries, sessions=sessions, projects_dir=projects_dir, project_root=root
    )
    growth_data = growth_report(entries, root, days=days, threshold=threshold, now=now)
    growth_files = growth_data.get("files", {})

    records: list[dict[str, Any]] = []
    total_tokens = 0
    never_recalled_count = 0

    for entry in entries:
        path = entry["path"]
        weight = weigh(Path(path), exact=exact, model=model, cache_dir=resolved_cache_dir)
        tokens = weight["tokens"]
        total_tokens += tokens

        recall = recall_stats.get(
            path, {"last_recalled": None, "recall_count": 0, "never_recalled": True}
        )
        if recall["never_recalled"]:
            never_recalled_count += 1

        try:
            rel_path = Path(path).relative_to(root).as_posix()
        except ValueError:
            # Entries outside root (e.g. user-source files pulled in via
            # user_home) can't be made root-relative; show the full path.
            rel_path = path

        growth_file = growth_files.get(rel_path)
        growth_flagged = bool(growth_file and growth_file.get("flagged"))
        growth_ratio = growth_file.get("ratio") if growth_file else None

        records.append(
            {
                "path": path,
                "rel_path": rel_path,
                "kind": entry["kind"],
                "source": entry["source"],
                "bytes": entry["bytes"],
                "tokens": tokens,
                "token_method": weight["method"],
                "error_pct": weight.get("error_pct", 0.0),
                "recall_count": recall["recall_count"],
                "last_recalled": recall["last_recalled"],
                "never_recalled": recall["never_recalled"],
                "growth_flagged": growth_flagged,
                "growth_ratio": growth_ratio,
            }
        )

    # Never-recalled first, then by token weight descending within each group.
    records.sort(key=lambda r: (0 if r["never_recalled"] else 1, -r["tokens"]))

    totals = {
        "total_tokens": total_tokens,
        "file_count": len(records),
        "never_recalled_count": never_recalled_count,
        "budget": budget,
        "budget_exceeded": bool(budget is not None and total_tokens > budget),
    }

    return {
        "meta": {
            "root": root.as_posix(),
            "generated_at": now.isoformat(),
            "sessions": sessions,
            "model": model,
            "exact": exact,
        },
        "records": records,
        "totals": totals,
        "growth": growth_data,
    }


def _fmt_tokens(record: dict[str, Any]) -> str:
    value = str(record["tokens"])
    if record["error_pct"]:
        value += f" (+/-{record['error_pct']:g}%)"
    return value


def _fmt_growth(record: dict[str, Any]) -> str:
    ratio = record["growth_ratio"]
    if ratio is None:
        return "!" if record["growth_flagged"] else ""
    marker = "!" if record["growth_flagged"] else ""
    return f"{ratio:.1f}x{marker}"


def format_human(report: dict[str, Any]) -> str:
    """Render ``build_report()``'s output as a plain-text table + summary.
    No color, no external deps — stdlib string formatting only."""
    meta = report["meta"]
    records = report["records"]
    totals = report["totals"]
    growth_data = report["growth"]

    lines: list[str] = [f"ctx-yield scan: {meta['root']}", ""]

    if not records:
        lines.append("No context files found.")
    else:
        header = (
            f"{'path':<48} {'kind':<10} {'tokens':>16} {'recalls':>8} "
            f"{'last recalled':<26} {'growth':>7}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for r in records:
            marker = "* " if r["never_recalled"] else "  "
            lines.append(
                f"{marker}{r['rel_path']:<46} {r['kind']:<10} {_fmt_tokens(r):>16} "
                f"{r['recall_count']:>8} {r['last_recalled'] or 'never':<26} "
                f"{_fmt_growth(r):>7}"
            )
        lines.append("(* = never recalled)")

    lines.append("")
    budget_line = f"Total force-load tokens: {totals['total_tokens']}"
    if totals["budget"] is not None:
        state = "OVER BUDGET" if totals["budget_exceeded"] else "under budget"
        budget_line += f" (budget {totals['budget']}, {state})"
    lines.append(budget_line)
    lines.append(f"Never-recalled files: {totals['never_recalled_count']}/{totals['file_count']}")

    total_growth = growth_data.get("total", {})
    if growth_data.get("is_git_repo") and total_growth.get("window_ratio") is not None:
        lines.append(
            f"Force-load grew {total_growth['window_ratio']:.1f}x "
            f"in {growth_data['window_days']} days"
        )
    elif not growth_data.get("is_git_repo"):
        lines.append("Growth: no git history found at this root (skipped).")

    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-yield",
        description="Measure whether an AI context system is earning its tokens.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser(
        "scan", help="Scan a project's context system and print a ranked report."
    )
    scan.add_argument(
        "path", nargs="?", default=".", help="Project root to scan (default: current directory)."
    )
    scan.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="N",
        help="Token budget; exit 1 when total force-load weight exceeds N.",
    )
    scan.add_argument(
        "--sessions",
        type=int,
        default=DEFAULT_SESSIONS,
        metavar="K",
        help=f"Number of recent session transcripts to join against (default: {DEFAULT_SESSIONS}).",
    )
    scan.add_argument(
        "--exact",
        action="store_true",
        help="Use the exact count_tokens API instead of the offline heuristic (needs ANTHROPIC_API_KEY).",
    )
    scan.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        metavar="MODEL",
        help=f"Tokenizer target model (default: {DEFAULT_MODEL}).",
    )
    scan.add_argument(
        "--json", action="store_true", help="Print one JSON document instead of the text report."
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        root = Path(args.path).resolve()
        report = build_report(
            root,
            budget=args.budget,
            sessions=args.sessions,
            exact=args.exact,
            model=args.model,
            user_home=_default_user_home(),
            projects_dir=_default_projects_dir(),
        )
    except KeyboardInterrupt:
        print("ctx-yield: interrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_human(report))

    return 1 if report["totals"]["budget_exceeded"] else 0


if __name__ == "__main__":
    sys.exit(main())
