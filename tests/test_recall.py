"""Tests for ctx_yield.recall.join().

All transcript content is synthetic, built directly in ``tmp_path`` — never
the real ``~/.claude`` — per the module's injectable projects_dir/project_root
contract (defaults to skipping transcript discovery entirely).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from ctx_yield.recall import join, _munge_project_path


def _write_jsonl(path: Path, lines: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            if isinstance(line, str):
                f.write(line + "\n")
            else:
                f.write(json.dumps(line) + "\n")


def _tool_use_entry(timestamp: str | None, tool_name: str, tool_input: dict) -> dict:
    entry = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": tool_name, "input": tool_input},
            ]
        },
    }
    if timestamp is not None:
        entry["timestamp"] = timestamp
    return entry


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    """A fake project root + a fake CLAUDE.md file inside it, plus a fake
    skill file. Returns (project_root, claude_md_path)."""
    project_root = tmp_path / "fake-project"
    project_root.mkdir()
    claude_md = project_root / "CLAUDE.md"
    claude_md.write_text("# fake project\n", encoding="utf-8")
    skill_dir = project_root / ".claude" / "skills"
    skill_dir.mkdir(parents=True)
    (skill_dir / "example-skill.md").write_text("skill body\n", encoding="utf-8")
    return project_root, claude_md


def _make_inventory(claude_md: Path, project_root: Path) -> list[dict]:
    skill_path = project_root / ".claude" / "skills" / "example-skill.md"
    never_touched = project_root / "AGENTS.md"
    never_touched.write_text("agents\n", encoding="utf-8")
    return [
        {
            "path": claude_md.resolve().as_posix(),
            "kind": "claude-md",
            "bytes": 10,
            "sha256": "x" * 64,
            "source": "project",
        },
        {
            "path": skill_path.resolve().as_posix(),
            "kind": "skill",
            "bytes": 5,
            "sha256": "y" * 64,
            "source": "project",
        },
        {
            "path": never_touched.resolve().as_posix(),
            "kind": "agents-md",
            "bytes": 6,
            "sha256": "z" * 64,
            "source": "project",
        },
    ]


def _make_session_dir(tmp_path: Path, project_root: Path) -> tuple[Path, Path]:
    """A fake <claude_home>/projects dir with the correctly-munged
    subdirectory for project_root. Returns (projects_dir, session_dir)."""
    projects_dir = tmp_path / "claude-home" / "projects"
    munged = _munge_project_path(project_root)
    session_dir = projects_dir / munged
    session_dir.mkdir(parents=True)
    return projects_dir, session_dir


def test_munge_matches_known_real_shape():
    # Verified against a real Claude Code installation (see module
    # docstring): separators/colon -> "-", drive letter lowercased.
    # _munge_project_path resolves its input, so only platform-native
    # absolute paths munge portably — assert the shape for this OS.
    if sys.platform == "win32":
        munged = _munge_project_path("C:/Users/x/Projects/ctx-yield")
        assert munged == "c--Users-x-Projects-ctx-yield"
    else:
        munged = _munge_project_path("/home/x/Projects/ctx-yield")
        assert munged == "-home-x-Projects-ctx-yield"


def test_no_projects_dir_or_project_root_marks_everything_never_recalled(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    result = join(inventory)
    assert len(result) == len(inventory)
    for entry in inventory:
        stat = result[entry["path"]]
        assert stat == {"last_recalled": None, "recall_count": 0, "never_recalled": True}


def test_read_tool_use_recalls_matching_inventory_file(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    projects_dir, session_dir = _make_session_dir(tmp_path, project_root)

    _write_jsonl(
        session_dir / "session1.jsonl",
        [
            _tool_use_entry(
                "2026-08-11T10:00:00.000Z",
                "Read",
                {"file_path": str(claude_md)},
            ),
        ],
    )

    result = join(inventory, projects_dir=projects_dir, project_root=project_root)
    claude_stat = result[claude_md.resolve().as_posix()]
    assert claude_stat["recall_count"] == 1
    assert claude_stat["never_recalled"] is False
    assert claude_stat["last_recalled"] == "2026-08-11T10:00:00.000Z"


def test_recall_count_accumulates_across_reads_and_edits(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    projects_dir, session_dir = _make_session_dir(tmp_path, project_root)

    _write_jsonl(
        session_dir / "session1.jsonl",
        [
            _tool_use_entry("2026-08-11T10:00:00.000Z", "Read", {"file_path": str(claude_md)}),
            _tool_use_entry("2026-08-11T10:05:00.000Z", "Edit", {"file_path": str(claude_md)}),
            _tool_use_entry("2026-08-11T10:10:00.000Z", "Write", {"file_path": str(claude_md)}),
        ],
    )

    result = join(inventory, projects_dir=projects_dir, project_root=project_root)
    assert result[claude_md.resolve().as_posix()]["recall_count"] == 3


def test_last_recalled_picks_the_latest_timestamp_regardless_of_line_order(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    projects_dir, session_dir = _make_session_dir(tmp_path, project_root)

    _write_jsonl(
        session_dir / "session1.jsonl",
        [
            _tool_use_entry("2026-08-11T09:00:00.000Z", "Read", {"file_path": str(claude_md)}),
            _tool_use_entry("2026-08-11T12:30:00.000Z", "Read", {"file_path": str(claude_md)}),
            _tool_use_entry("2026-08-11T10:00:00.000Z", "Read", {"file_path": str(claude_md)}),
        ],
    )

    result = join(inventory, projects_dir=projects_dir, project_root=project_root)
    assert result[claude_md.resolve().as_posix()]["last_recalled"] == "2026-08-11T12:30:00.000Z"


def test_never_recalled_file_keeps_null_last_recalled_and_flag(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    projects_dir, session_dir = _make_session_dir(tmp_path, project_root)

    _write_jsonl(
        session_dir / "session1.jsonl",
        [_tool_use_entry("2026-08-11T10:00:00.000Z", "Read", {"file_path": str(claude_md)})],
    )

    result = join(inventory, projects_dir=projects_dir, project_root=project_root)
    agents_md_path = next(e["path"] for e in inventory if e["kind"] == "agents-md")
    stat = result[agents_md_path]
    assert stat == {"last_recalled": None, "recall_count": 0, "never_recalled": True}


def test_skill_tool_use_matches_inventory_skill_by_stem(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    projects_dir, session_dir = _make_session_dir(tmp_path, project_root)

    _write_jsonl(
        session_dir / "session1.jsonl",
        [
            _tool_use_entry(
                "2026-08-11T11:00:00.000Z",
                "Skill",
                {"skill": "ns:example-skill", "args": "whatever"},
            ),
        ],
    )

    result = join(inventory, projects_dir=projects_dir, project_root=project_root)
    skill_path = next(e["path"] for e in inventory if e["kind"] == "skill")
    stat = result[skill_path]
    assert stat["recall_count"] == 1
    assert stat["never_recalled"] is False
    assert stat["last_recalled"] == "2026-08-11T11:00:00.000Z"


def test_malformed_lines_are_skipped_not_raised(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    projects_dir, session_dir = _make_session_dir(tmp_path, project_root)

    _write_jsonl(
        session_dir / "session1.jsonl",
        [
            "{not valid json",
            "42",  # valid JSON, but not a dict
            "[1, 2, 3]",  # valid JSON, but not a dict
            "",  # blank line
            _tool_use_entry("2026-08-11T10:00:00.000Z", "Read", {"file_path": str(claude_md)}),
        ],
    )

    result = join(inventory, projects_dir=projects_dir, project_root=project_root)  # must not raise
    assert result[claude_md.resolve().as_posix()]["recall_count"] == 1


def test_missing_timestamp_falls_back_to_session_file_mtime(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    projects_dir, session_dir = _make_session_dir(tmp_path, project_root)

    session_file = session_dir / "session1.jsonl"
    _write_jsonl(
        session_file,
        [_tool_use_entry(None, "Read", {"file_path": str(claude_md)})],
    )

    result = join(inventory, projects_dir=projects_dir, project_root=project_root)
    stat = result[claude_md.resolve().as_posix()]
    assert stat["recall_count"] == 1
    assert stat["last_recalled"] is not None
    # No per-entry timestamp was present, so the recorded value must be the
    # file's own mtime-derived ISO string, not the wall-clock "now".
    from ctx_yield.recall import _iso_from_mtime

    assert stat["last_recalled"] == _iso_from_mtime(session_file.stat().st_mtime)


def test_k_window_limits_which_sessions_count(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    projects_dir, session_dir = _make_session_dir(tmp_path, project_root)

    # Three sessions, written oldest -> newest so mtimes order predictably.
    old_session = session_dir / "session-old.jsonl"
    mid_session = session_dir / "session-mid.jsonl"
    new_session = session_dir / "session-new.jsonl"

    _write_jsonl(
        old_session,
        [_tool_use_entry("2026-08-01T00:00:00.000Z", "Read", {"file_path": str(claude_md)})],
    )
    now = time.time()
    os.utime(old_session, (now - 300, now - 300))

    _write_jsonl(
        mid_session,
        [_tool_use_entry("2026-08-05T00:00:00.000Z", "Read", {"file_path": str(claude_md)})],
    )
    os.utime(mid_session, (now - 200, now - 200))

    _write_jsonl(
        new_session,
        [_tool_use_entry("2026-08-10T00:00:00.000Z", "Read", {"file_path": str(claude_md)})],
    )
    os.utime(new_session, (now - 100, now - 100))

    # sessions=2 -> only mid + new count; old (recall #1) is out of window.
    result = join(inventory, sessions=2, projects_dir=projects_dir, project_root=project_root)
    stat = result[claude_md.resolve().as_posix()]
    assert stat["recall_count"] == 2
    assert stat["last_recalled"] == "2026-08-10T00:00:00.000Z"

    # sessions=1 -> only new counts.
    result_one = join(inventory, sessions=1, projects_dir=projects_dir, project_root=project_root)
    stat_one = result_one[claude_md.resolve().as_posix()]
    assert stat_one["recall_count"] == 1
    assert stat_one["last_recalled"] == "2026-08-10T00:00:00.000Z"


def test_case_insensitive_path_match_on_windows_style_paths(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    projects_dir, session_dir = _make_session_dir(tmp_path, project_root)

    # Transcript records the path with different case than the inventory.
    weird_case_path = str(claude_md).upper()
    _write_jsonl(
        session_dir / "session1.jsonl",
        [_tool_use_entry("2026-08-11T10:00:00.000Z", "Read", {"file_path": weird_case_path})],
    )

    result = join(inventory, projects_dir=projects_dir, project_root=project_root)
    assert result[claude_md.resolve().as_posix()]["recall_count"] == 1


def test_session_dir_lookup_tolerates_case_mismatch(tmp_path):
    """Munging is lossy; the fallback scan must find a subdir that only
    differs from the computed munged name by case."""
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)

    projects_dir = tmp_path / "claude-home" / "projects"
    munged = _munge_project_path(project_root)
    weird_case_dir = projects_dir / munged.upper()
    weird_case_dir.mkdir(parents=True)

    _write_jsonl(
        weird_case_dir / "session1.jsonl",
        [_tool_use_entry("2026-08-11T10:00:00.000Z", "Read", {"file_path": str(claude_md)})],
    )

    result = join(inventory, projects_dir=projects_dir, project_root=project_root)
    assert result[claude_md.resolve().as_posix()]["recall_count"] == 1


def test_every_inventory_entry_gets_exactly_one_result_key(tmp_path):
    project_root, claude_md = _make_project(tmp_path)
    inventory = _make_inventory(claude_md, project_root)
    result = join(inventory)
    assert set(result.keys()) == {e["path"] for e in inventory}
