"""Git-history growth: is the corpus getting heavier over time?

Walks ``git log --numstat --follow`` for each corpus file (as discovered by
:func:`ctx_yield.corpus.inventory`) and builds a cumulative size series per
file plus a total-corpus series, then flags files that grew more than
``threshold``x over a trailing window.

**Size proxy**: git ``--numstat`` reports added/deleted *lines*, not bytes or
tokens — there is no cheap way to recover historical byte/token counts for
every past revision of a file without checking out each blob. Lines are used
as the growth proxy throughout this module; document this to any caller that
expects a byte or token figure (``tokens.py`` gives you the *current*
snapshot's real token weight — this module only tells you the *shape* of
growth over time).

Public interface: :func:`growth`. See ``interface.md`` (repo root,
gitignored coordination doc) for the realized field-by-field contract; the
shape is also fully pinned down by ``tests/test_growth.py``.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

#: Default trailing window (days) the growth flag looks back over.
DEFAULT_WINDOW_DAYS = 90

#: Default growth ratio that trips the "flagged" bit.
DEFAULT_THRESHOLD = 2.0

#: Per-subprocess-call timeout (seconds). ``git log`` on a single pathspec is
#: normally instant; this exists so a pathological repo (huge history, a
#: network-mounted .git) degrades to a per-file error marker instead of
#: hanging the whole run.
DEFAULT_GIT_TIMEOUT = 30.0

# Unambiguous per-commit header marker, followed by hash and author date
# (ISO 8601, %aI) tab-separated. Numstat lines are always "<added>\t<deleted>\t<path>"
# (or "-\t-\t<path>" for a binary diff) and never start with this marker, so
# a plain string-prefix check disambiguates header lines from numstat lines.
_HEADER_MARKER = "\x01COMMIT"
_LOG_FORMAT = f"{_HEADER_MARKER}%x09%H%x09%aI"


def _run_git(args: list[str], cwd: Path, timeout: float) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # git not installed, or this call hung past the timeout — never
        # crash the caller for either.
        return None


def _is_git_repo(root: Path, timeout: float) -> bool:
    result = _run_git(["rev-parse", "--is-inside-work-tree"], root, timeout)
    return bool(result) and result.returncode == 0 and result.stdout.strip() == "true"


def _relpath_in_repo(entry_path_posix: str, root_posix: str) -> Optional[str]:
    """Strip ``root_posix`` off an absolute POSIX-style corpus path.

    Returns ``None`` (meaning: not part of this repo, skip it) if the entry
    doesn't live under ``root`` at all — this is how user-source entries
    (outside the project directory) get excluded, per the corpus interface
    note for this module.
    """
    prefix = root_posix.rstrip("/") + "/"
    if not entry_path_posix.startswith(prefix):
        return None
    return entry_path_posix[len(prefix):]


def _parse_numstat_log(output: str) -> list[dict[str, Any]]:
    """Parse ``git log --numstat --format=<_LOG_FORMAT>`` output into a list
    of ``{"sha", "date", "added", "deleted", "binary"}`` dicts, one per
    commit that touched the pathspec, in the order git emitted them
    (newest first)."""
    commits: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None

    for line in output.splitlines():
        if line.startswith(_HEADER_MARKER):
            rest = line[len(_HEADER_MARKER):]
            parts = rest.split("\t")
            # parts[0] is "" (the %x09 right after the marker), then sha, date.
            if len(parts) >= 3:
                sha, date_str = parts[1], parts[2]
            else:
                continue
            current = {
                "sha": sha,
                "date": datetime.fromisoformat(date_str),
                "added": 0,
                "deleted": 0,
                "binary": False,
            }
            commits.append(current)
            continue

        if current is None or not line.strip():
            continue

        fields = line.split("\t")
        if len(fields) < 2:
            continue
        added_raw, deleted_raw = fields[0], fields[1]
        if added_raw == "-" or deleted_raw == "-":
            # Binary file: numstat can't report line deltas for it. Mark it
            # and contribute nothing to the line-count series for this commit.
            current["binary"] = True
            continue
        try:
            current["added"] += int(added_raw)
            current["deleted"] += int(deleted_raw)
        except ValueError:
            continue

    return commits


def _build_series(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chronological (oldest-first) cumulative-lines series from parsed
    commits (which arrive newest-first from ``git log``)."""
    series: list[dict[str, Any]] = []
    cumulative = 0
    for commit in reversed(commits):
        cumulative = max(0, cumulative + commit["added"] - commit["deleted"])
        series.append(
            {
                "date": commit["date"].isoformat(),
                "sha": commit["sha"],
                "added": commit["added"],
                "deleted": commit["deleted"],
                "binary": commit["binary"],
                "cumulative_lines": cumulative,
            }
        )
    return series


def _size_at(series: list[dict[str, Any]], when: datetime) -> int:
    """Cumulative line count as of the last series point at or before
    ``when``; 0 if the file has no series points that early (didn't exist
    yet, or has no history at all)."""
    size = 0
    for point in series:
        point_date = datetime.fromisoformat(point["date"])
        if point_date <= when:
            size = point["cumulative_lines"]
        else:
            break
    return size


def _file_growth(
    relpath: str,
    root: Path,
    days: int,
    threshold: float,
    now: datetime,
    timeout: float,
) -> dict[str, Any]:
    result = _run_git(
        ["log", "--follow", "--numstat", f"--format={_LOG_FORMAT}", "--", relpath],
        root,
        timeout,
    )
    if result is None:
        return {
            "tracked": False,
            "reason": "git log call failed or timed out",
            "series": [],
            "size_now": 0,
            "size_window_start": None,
            "ratio": None,
            "flagged": False,
            "first_commit_date": None,
        }
    if result.returncode != 0:
        return {
            "tracked": False,
            "reason": f"git log exited {result.returncode}: {result.stderr.strip()}",
            "series": [],
            "size_now": 0,
            "size_window_start": None,
            "ratio": None,
            "flagged": False,
            "first_commit_date": None,
        }

    commits = _parse_numstat_log(result.stdout)
    if not commits:
        return {
            "tracked": False,
            "reason": "not tracked in git (no commit history for this path)",
            "series": [],
            "size_now": 0,
            "size_window_start": None,
            "ratio": None,
            "flagged": False,
            "first_commit_date": None,
        }

    series = _build_series(commits)
    size_now = _size_at(series, now)
    window_start = now - timedelta(days=days)
    size_window_start = _size_at(series, window_start)
    first_commit_date = series[0]["date"]

    # size_window_start is 0 both when the file didn't exist yet at the
    # window start, and (degenerately) when it existed with zero lines.
    # Either way a ratio can't be computed from a zero base; treat any
    # nonzero size_now as flagged growth in that case rather than dividing.
    had_size_at_window_start = size_window_start > 0
    ratio = (size_now / size_window_start) if had_size_at_window_start else None
    flagged = (
        (ratio is not None and ratio > threshold)
        or (not had_size_at_window_start and size_now > 0)
    )

    return {
        "tracked": True,
        "reason": None,
        "series": series,
        "size_now": size_now,
        "size_window_start": size_window_start if had_size_at_window_start else None,
        "ratio": ratio,
        "flagged": flagged,
        "first_commit_date": first_commit_date,
    }


def _empty_total() -> dict[str, Any]:
    return {
        "size_now": 0,
        "size_window_start": 0,
        "window_ratio": None,
        "window_flagged": False,
        "size_full_history_start": 0,
        "full_history_ratio": None,
        "full_history_days": None,
        "files_tracked": 0,
        "files_skipped": 0,
    }


def growth(
    inventory: list[dict[str, Any]],
    root: str | Path,
    days: int = DEFAULT_WINDOW_DAYS,
    threshold: float = DEFAULT_THRESHOLD,
    now: Optional[datetime] = None,
    timeout: float = DEFAULT_GIT_TIMEOUT,
) -> dict[str, Any]:
    """Measure git-history growth for the corpus files in ``inventory``.

    Args:
        inventory: the ``list[dict]`` returned by ``ctx_yield.corpus.inventory()``.
            Entries whose ``path`` doesn't live under ``root`` (e.g. user-home
            entries) are silently skipped — they aren't part of this repo's
            history.
        root: the project directory that is (or should be) a git repo. Must
            be the same ``root`` passed to ``inventory()``, so corpus paths
            can be stripped down to repo-relative pathspecs.
        days: trailing-window size (days) for the growth-flag comparison.
        threshold: ratio (size_now / size_window_start) above which a file
            (or the total corpus) is flagged as having grown.
        now: reference "current time" (timezone-aware recommended). Defaults
            to ``datetime.now(timezone.utc)``. Pin this in tests for a
            deterministic window.
        timeout: per-``git log`` subprocess timeout in seconds.

    Returns:
        ``{"repo_root", "is_git_repo", "now", "window_days", "threshold",
        "files": {<repo-relative posix path>: {...}}, "total": {...}}``.
        See ``interface.md`` for the full field-by-field shape.

        If ``root`` is not a git repository (or ``git`` itself isn't on
        PATH), returns the same top-level shape with ``is_git_repo: False``,
        ``files: {}``, and a zeroed-out ``total`` — never raises.
    """
    root = Path(root).resolve()
    if now is None:
        now = datetime.now(timezone.utc)

    base = {
        "repo_root": root.as_posix(),
        "now": now.isoformat(),
        "window_days": days,
        "threshold": threshold,
    }

    if not _is_git_repo(root, timeout):
        return {
            **base,
            "is_git_repo": False,
            "files": {},
            "total": _empty_total(),
        }

    root_posix = root.as_posix()
    files: dict[str, dict[str, Any]] = {}
    seen_relpaths: set[str] = set()

    for entry in inventory:
        relpath = _relpath_in_repo(entry["path"], root_posix)
        if relpath is None or relpath in seen_relpaths:
            continue
        seen_relpaths.add(relpath)
        files[relpath] = _file_growth(relpath, root, days, threshold, now, timeout)

    window_start = now - timedelta(days=days)
    size_now_total = sum(f["size_now"] for f in files.values() if f["tracked"])
    size_window_start_total = sum(
        (f["size_window_start"] or 0) for f in files.values() if f["tracked"]
    )
    size_full_history_start_total = sum(
        f["series"][0]["cumulative_lines"] for f in files.values() if f["tracked"] and f["series"]
    )
    files_tracked = sum(1 for f in files.values() if f["tracked"])
    files_skipped = sum(1 for f in files.values() if not f["tracked"])

    window_ratio = (
        size_now_total / size_window_start_total if size_window_start_total > 0 else None
    )
    window_flagged = (
        (window_ratio is not None and window_ratio > threshold)
        or (size_window_start_total == 0 and size_now_total > 0)
    )

    first_dates = [
        datetime.fromisoformat(f["first_commit_date"])
        for f in files.values()
        if f["tracked"] and f["first_commit_date"]
    ]
    if first_dates:
        earliest = min(first_dates)
        full_history_days = (now - earliest).total_seconds() / 86400.0
    else:
        full_history_days = None

    full_history_ratio = (
        size_now_total / size_full_history_start_total
        if size_full_history_start_total > 0
        else None
    )

    total = {
        "size_now": size_now_total,
        "size_window_start": size_window_start_total,
        "window_ratio": window_ratio,
        "window_flagged": window_flagged,
        "size_full_history_start": size_full_history_start_total,
        "full_history_ratio": full_history_ratio,
        "full_history_days": full_history_days,
        "files_tracked": files_tracked,
        "files_skipped": files_skipped,
    }

    return {
        **base,
        "is_git_repo": True,
        "files": files,
        "total": total,
    }
