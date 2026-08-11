"""Tests for ctx_yield.growth.growth().

Builds real temp git repos under tmp_path (neutral "Test" committer identity,
pinned commit dates via GIT_AUTHOR_DATE/GIT_COMMITTER_DATE) so the growth
series and flag math can be asserted against known numbers.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ctx_yield.growth import growth

ANCHOR = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _git(cwd: Path, *args: str, date: datetime | None = None) -> None:
    env = dict(os.environ)
    if date is not None:
        iso = date.isoformat()
        env["GIT_AUTHOR_DATE"] = iso
        env["GIT_COMMITTER_DATE"] = iso
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=str(cwd),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _write_lines(path: Path, n: int) -> None:
    path.write_text("\n".join(f"line {i}" for i in range(n)) + "\n", encoding="utf-8")


def _inventory_entry(path: Path) -> dict:
    return {
        "path": path.resolve().as_posix(),
        "kind": "claude-md",
        "bytes": len(path.read_bytes()),
        "sha256": "0" * 64,
        "source": "project",
    }


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")


def test_series_and_growth_flag_with_pinned_now(tmp_path: Path):
    _init_repo(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"

    # commit 1: anchor, 10 lines
    _write_lines(claude_md, 10)
    _git(tmp_path, "add", "CLAUDE.md")
    _git(tmp_path, "commit", "-q", "-m", "c1", date=ANCHOR)

    # commit 2: anchor + 40d, 30 lines (+20)
    _write_lines(claude_md, 30)
    _git(tmp_path, "add", "CLAUDE.md")
    _git(tmp_path, "commit", "-q", "-m", "c2", date=ANCHOR + timedelta(days=40))

    # commit 3: anchor + 100d, 100 lines (+70)
    _write_lines(claude_md, 100)
    _git(tmp_path, "add", "CLAUDE.md")
    _git(tmp_path, "commit", "-q", "-m", "c3", date=ANCHOR + timedelta(days=100))

    now = ANCHOR + timedelta(days=130)
    inventory = [_inventory_entry(claude_md)]

    result = growth(inventory, tmp_path, days=90, threshold=2.0, now=now)

    assert result["is_git_repo"] is True
    file_result = result["files"]["CLAUDE.md"]
    assert file_result["tracked"] is True

    # Series is chronological with the right cumulative sizes.
    sizes = [point["cumulative_lines"] for point in file_result["series"]]
    assert sizes == [10, 30, 100]

    # window_start = now - 90d = anchor + 40d == commit 2 exactly.
    assert file_result["size_window_start"] == 30
    assert file_result["size_now"] == 100
    assert file_result["ratio"] == pytest.approx(100 / 30)
    assert file_result["flagged"] is True  # ratio ~3.33 > threshold 2.0

    total = result["total"]
    assert total["size_now"] == 100
    assert total["size_window_start"] == 30
    assert total["window_flagged"] is True
    assert total["size_full_history_start"] == 10
    assert total["full_history_ratio"] == pytest.approx(10.0)
    assert total["full_history_days"] == pytest.approx(130.0)
    assert total["files_tracked"] == 1
    assert total["files_skipped"] == 0


def test_below_threshold_is_not_flagged(tmp_path: Path):
    _init_repo(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    _write_lines(claude_md, 10)
    _git(tmp_path, "add", "CLAUDE.md")
    _git(tmp_path, "commit", "-q", "-m", "c1", date=ANCHOR)

    _write_lines(claude_md, 15)  # 1.5x, under default 2.0 threshold
    _git(tmp_path, "add", "CLAUDE.md")
    _git(tmp_path, "commit", "-q", "-m", "c2", date=ANCHOR + timedelta(days=1))

    # window=2d puts window_start exactly at commit 1 (10 lines); ratio
    # 15/10 = 1.5x sits under the default 2.0 threshold.
    now = ANCHOR + timedelta(days=2)
    result = growth([_inventory_entry(claude_md)], tmp_path, days=2, now=now)
    file_result = result["files"]["CLAUDE.md"]
    assert file_result["ratio"] == pytest.approx(1.5)
    assert file_result["flagged"] is False


def test_untracked_file_gets_a_marker_not_a_crash(tmp_path: Path):
    _init_repo(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    _write_lines(claude_md, 5)
    _git(tmp_path, "add", "CLAUDE.md")
    _git(tmp_path, "commit", "-q", "-m", "c1", date=ANCHOR)

    untracked = tmp_path / "untracked.md"
    _write_lines(untracked, 5)  # written but never git-added/committed

    inventory = [_inventory_entry(claude_md), _inventory_entry(untracked)]
    result = growth(inventory, tmp_path, now=ANCHOR + timedelta(days=1))

    untracked_result = result["files"]["untracked.md"]
    assert untracked_result["tracked"] is False
    assert "not tracked" in untracked_result["reason"]
    assert untracked_result["series"] == []
    assert untracked_result["flagged"] is False
    assert result["total"]["files_skipped"] == 1
    assert result["total"]["files_tracked"] == 1


def test_nonrepo_root_returns_empty_result_no_crash(tmp_path: Path):
    # tmp_path is a plain directory, never `git init`-ed.
    fake_md = tmp_path / "CLAUDE.md"
    _write_lines(fake_md, 5)
    result = growth([_inventory_entry(fake_md)], tmp_path)

    assert result["is_git_repo"] is False
    assert result["files"] == {}
    assert result["total"]["size_now"] == 0
    assert result["total"]["window_ratio"] is None
    assert result["total"]["window_flagged"] is False


def test_follow_tracks_history_across_a_rename(tmp_path: Path):
    _init_repo(tmp_path)
    original = tmp_path / "OLD.md"
    _write_lines(original, 10)
    _git(tmp_path, "add", "OLD.md")
    _git(tmp_path, "commit", "-q", "-m", "c1", date=ANCHOR)

    renamed = tmp_path / "NEW.md"
    _git(tmp_path, "mv", "OLD.md", "NEW.md")
    # Small edit (not a full rewrite) so git's rename similarity heuristic
    # (default ~50%) actually recognizes this as a rename, not a delete+add.
    _write_lines(renamed, 12)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "c2 rename+grow", date=ANCHOR + timedelta(days=1))

    result = growth(
        [_inventory_entry(renamed)], tmp_path, now=ANCHOR + timedelta(days=2)
    )
    file_result = result["files"]["NEW.md"]
    assert file_result["tracked"] is True
    # --follow must see both commits (the pre-rename one under OLD.md too).
    assert len(file_result["series"]) == 2
    assert [p["cumulative_lines"] for p in file_result["series"]] == [10, 12]


def test_binary_file_deltas_are_skipped_not_crashed(tmp_path: Path):
    _init_repo(tmp_path)
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02binarydata")
    _git(tmp_path, "add", "blob.bin")
    _git(tmp_path, "commit", "-q", "-m", "c1", date=ANCHOR)

    result = growth([_inventory_entry(binary)], tmp_path, now=ANCHOR + timedelta(days=1))
    file_result = result["files"]["blob.bin"]
    assert file_result["tracked"] is True
    assert file_result["series"][0]["cumulative_lines"] == 0
    assert file_result["series"][0]["binary"] is True


def test_user_source_entries_outside_root_are_skipped(tmp_path: Path):
    _init_repo(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    _write_lines(claude_md, 5)
    _git(tmp_path, "add", "CLAUDE.md")
    _git(tmp_path, "commit", "-q", "-m", "c1", date=ANCHOR)

    outside = tmp_path.parent / f"outside-{tmp_path.name}.md"
    outside.write_text("hello", encoding="utf-8")
    try:
        inventory = [_inventory_entry(claude_md), _inventory_entry(outside)]
        result = growth(inventory, tmp_path, now=ANCHOR + timedelta(days=1))
        assert list(result["files"].keys()) == ["CLAUDE.md"]
    finally:
        outside.unlink()
