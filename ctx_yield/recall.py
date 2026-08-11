"""Corpus x transcript join: was a context file actually recalled?

Reads Claude Code session transcripts (``<claude_home>/projects/<munged
project path>/*.jsonl``) and joins them against a corpus inventory (see
``ctx_yield.corpus.inventory``) to compute, per inventory file, whether and
when it was last recalled across the last ``sessions`` sessions.

Public interface: :func:`join`. See ``interface.md`` (repo root, gitignored
coordination doc) for the realized field-by-field contract; the shape is
also fully pinned down by ``tests/test_recall.py``.

## What counts as "recalled" (best-effort, documented here)

- A ``tool_use`` block named ``Read``, ``Edit``, or ``Write`` whose
  ``input.file_path`` resolves to the same file as an inventory entry
  (path comparison is case-insensitive, since Windows paths are).
- A ``tool_use`` block named ``Skill`` whose ``input.skill`` value (after
  stripping a ``namespace:`` prefix, e.g. ``"sia:end-session"`` ->
  ``"end-session"``) matches an inventory entry of kind ``skill`` or
  ``command`` by file stem — either the file's own stem (``foo.md`` ->
  ``foo``) or, for the ``<dir>/SKILL.md`` convention, the parent directory
  name. This is intentionally best-effort: it cannot see which specific
  file inside a multi-file skill/command bundle was actually read, only
  that the skill/command was invoked.

Every transcript line is parsed defensively: malformed JSON and lines that
don't decode to a dict are skipped outright, never raised.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# Tool names whose input.file_path is treated as "this file was read".
_FILE_TOOL_NAMES = frozenset({"Read", "Edit", "Write"})


def _munge_project_path(root: str | Path) -> str:
    """Compute Claude Code's munged transcript-directory name for a project
    root: the absolute path with ``/`` and ``:`` swapped to ``-`` and the
    drive letter (if any) lowercased, e.g.
    ``C:\\Users\\x\\Projects\\ctx-yield`` -> ``c--Users-x-Projects-ctx-yield``.
    Verified against a real installation; see module docstring."""
    posix = Path(root).resolve().as_posix()
    if len(posix) >= 2 and posix[1] == ":":
        posix = posix[0].lower() + posix[1:]
    return re.sub(r"[/:\\]", "-", posix)


def _find_session_dir(projects_dir: Path, project_root: Path) -> Path | None:
    """Locate the transcript directory for ``project_root`` under
    ``projects_dir``. Munging is lossy, so try the exact computed name
    first, then fall back to a case-insensitive scan of ``projects_dir``'s
    immediate subdirectories."""
    if not projects_dir.is_dir():
        return None
    candidate_name = _munge_project_path(project_root)
    exact = projects_dir / candidate_name
    if exact.is_dir():
        return exact
    candidate_lower = candidate_name.lower()
    for child in projects_dir.iterdir():
        if child.is_dir() and child.name.lower() == candidate_lower:
            return child
    return None


def _select_recent_sessions(session_dir: Path, k: int) -> list[Path]:
    """The ``k`` most recent ``*.jsonl`` files in ``session_dir``, newest
    first, by modification time."""
    files = [p for p in session_dir.glob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:k]


def _normalize_path(path_str: str) -> str:
    """Resolve to an absolute POSIX-style string and casefold, so Windows
    paths compare equal regardless of case or separator style. Never
    raises: an unparseable/nonexistent path still normalizes as best it
    can rather than blowing up the whole join."""
    try:
        resolved = Path(path_str).resolve(strict=False).as_posix()
    except (OSError, ValueError):
        resolved = Path(path_str).as_posix()
    return resolved.casefold()


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp, tolerating a trailing 'Z' (as Claude
    Code transcripts use) that :func:`datetime.fromisoformat` alone
    doesn't accept on every supported Python."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _iso_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _parse_line(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _tool_use_blocks(entry: dict):
    message = entry.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def _skill_key(skill_value: str) -> str:
    """"sia:end-session" -> "end-session"; a bare skill name passes
    through unchanged."""
    return skill_value.rsplit(":", 1)[-1].rsplit("/", 1)[-1].strip().casefold()


def _build_skill_index(inventory: list[dict]) -> dict[str, list[str]]:
    """kind in {skill, command} entries, keyed by matchable stem -> list of
    inventory paths sharing that stem (best-effort; see module docstring)."""
    index: dict[str, list[str]] = {}
    for entry in inventory:
        if entry.get("kind") not in ("skill", "command"):
            continue
        p = Path(entry["path"])
        stem = p.stem.casefold()
        if stem == "skill":
            # <dir>/SKILL.md convention: the skill name is the parent dir.
            stem = p.parent.name.casefold()
        index.setdefault(stem, []).append(entry["path"])
    return index


def _match_block(
    block: dict, path_index: dict[str, str], skill_index: dict[str, list[str]]
) -> list[str]:
    """Inventory paths (original, un-normalized) this tool_use block
    recalls, if any."""
    name = block.get("name")
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return []

    if name in _FILE_TOOL_NAMES:
        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return []
        matched = path_index.get(_normalize_path(file_path))
        return [matched] if matched else []

    if name == "Skill":
        skill = tool_input.get("skill")
        if not isinstance(skill, str) or not skill:
            return []
        return list(skill_index.get(_skill_key(skill), ()))

    return []


def join(
    inventory: list[dict],
    sessions: int = 30,
    projects_dir: str | Path | None = None,
    project_root: str | Path | None = None,
) -> dict[str, dict]:
    """Join a corpus inventory against Claude Code session transcripts.

    Args:
        inventory: the ``list[dict]`` returned by
            ``ctx_yield.corpus.inventory()`` (only the ``path`` and
            ``kind`` fields are used).
        sessions: how many of the most recent ``.jsonl`` transcripts
            (by file modification time) to scan. Default 30.
        projects_dir: the ``<claude_home>/projects`` directory (i.e. the
            parent of the per-project munged directories) — **not** a
            specific project's transcript dir. Defaults to ``None``, which
            skips transcript discovery entirely (every entry comes back
            "never recalled") so tests / library callers never touch a
            real ``~/.claude`` by accident. Callers building the real CLI
            must pass the real ``<claude_home>/projects`` explicitly.
        project_root: the project's root directory, used to compute the
            munged transcript-subdirectory name. Also defaults to
            ``None`` (same reasoning as ``projects_dir``); both must be
            provided together to get real transcript coverage.

    Returns:
        A dict keyed by each inventory entry's ``path`` (every inventory
        entry gets exactly one key, even if never recalled), each value
        shaped ``{"last_recalled": str | None, "recall_count": int,
        "never_recalled": bool}``. ``last_recalled`` is an ISO 8601
        string (the transcript entry's own ``timestamp`` field when
        present, else the whole session file's modification time as a
        fallback) or ``None`` if never recalled.
    """
    result: dict[str, dict] = {
        entry["path"]: {"last_recalled": None, "recall_count": 0, "never_recalled": True}
        for entry in inventory
    }
    if projects_dir is None or project_root is None:
        return result

    session_dir = _find_session_dir(Path(projects_dir), Path(project_root))
    if session_dir is None:
        return result

    path_index = {_normalize_path(e["path"]): e["path"] for e in inventory}
    skill_index = _build_skill_index(inventory)

    for session_file in _select_recent_sessions(session_dir, sessions):
        try:
            session_mtime_iso = _iso_from_mtime(session_file.stat().st_mtime)
            text = session_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for raw_line in text.splitlines():
            entry = _parse_line(raw_line)
            if entry is None:
                continue

            ts = entry.get("timestamp")
            if not isinstance(ts, str) or not ts:
                ts = session_mtime_iso
            try:
                parsed_ts = _parse_ts(ts)
            except ValueError:
                ts, parsed_ts = session_mtime_iso, _parse_ts(session_mtime_iso)

            for block in _tool_use_blocks(entry):
                for matched_path in _match_block(block, path_index, skill_index):
                    stat = result[matched_path]
                    stat["recall_count"] += 1
                    stat["never_recalled"] = False
                    if stat["last_recalled"] is None or parsed_ts > _parse_ts(
                        stat["last_recalled"]
                    ):
                        stat["last_recalled"] = ts

    return result
