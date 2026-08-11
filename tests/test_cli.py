"""Tests for ctx_yield.cli.

Uses only tests/fixtures/fake-project (read-only) — never a real ~/.claude
and never the network. An autouse fixture monkeypatches
cli._default_user_home / cli._default_projects_dir to throwaway tmp_path
directories so even a test that calls main() (rather than build_report()
directly) cannot reach the real home directory. --exact is only exercised
with ANTHROPIC_API_KEY explicitly unset, which short-circuits before any
network call per tokens.weigh()'s own contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctx_yield import cli
from ctx_yield.recall import _munge_project_path
from ctx_yield.tokens import MODEL_ERROR_PCT

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_PROJECT = FIXTURES / "fake-project"


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _read_entry(timestamp: str, file_path: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": file_path}}
            ]
        },
    }


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """No test in this file may ever resolve to a real ~/.claude."""
    fake_home = tmp_path / "isolated-home"
    fake_home.mkdir()
    monkeypatch.setattr(cli, "_default_user_home", lambda: fake_home)
    monkeypatch.setattr(cli, "_default_projects_dir", lambda: fake_home / ".claude" / "projects")


def _projects_dir_with_one_recall(tmp_path: Path, recalled_file: Path) -> Path:
    """A fake <claude_home>/projects dir with one session transcript that
    reads exactly recalled_file, correctly munged for FAKE_PROJECT."""
    projects_dir = tmp_path / "claude-home" / "projects"
    session_dir = projects_dir / _munge_project_path(FAKE_PROJECT)
    _write_jsonl(
        session_dir / "session1.jsonl",
        [_read_entry("2026-08-11T10:00:00.000Z", str(recalled_file))],
    )
    return projects_dir


# --------------------------------------------------------------------------
# Fixture sanity
# --------------------------------------------------------------------------


def test_fixture_sanity():
    assert FAKE_PROJECT.is_dir()
    assert (FAKE_PROJECT / "CLAUDE.md").is_file()


# --------------------------------------------------------------------------
# Ranking: never-recalled first, then by token weight descending
# --------------------------------------------------------------------------


def test_records_rank_never_recalled_first_then_by_tokens_desc(tmp_path):
    recalled_file = FAKE_PROJECT / "CLAUDE.md"
    projects_dir = _projects_dir_with_one_recall(tmp_path, recalled_file)

    report = cli.build_report(
        FAKE_PROJECT, projects_dir=projects_dir, cache_dir=tmp_path / "cache"
    )
    records = report["records"]
    assert len(records) >= 2  # the fixture has several context files

    never_recalled_flags = [r["never_recalled"] for r in records]
    # Exactly one recalled record (CLAUDE.md); everything else in the
    # fixture was never touched by the fake transcript.
    assert never_recalled_flags.count(False) == 1
    first_recalled_index = never_recalled_flags.index(False)

    # Every never-recalled record precedes the recalled one.
    assert all(never_recalled_flags[:first_recalled_index])
    assert records[first_recalled_index]["rel_path"] == "CLAUDE.md"
    assert first_recalled_index == len(records) - 1

    # Within the never-recalled group, sorted by token weight descending.
    never_recalled_tokens = [r["tokens"] for r in records[:first_recalled_index]]
    assert never_recalled_tokens == sorted(never_recalled_tokens, reverse=True)


def test_human_table_marks_never_recalled_rows_and_lists_them_first(tmp_path):
    recalled_file = FAKE_PROJECT / "CLAUDE.md"
    projects_dir = _projects_dir_with_one_recall(tmp_path, recalled_file)
    report = cli.build_report(
        FAKE_PROJECT, projects_dir=projects_dir, cache_dir=tmp_path / "cache"
    )
    text = cli.format_human(report)
    row_lines = [
        line
        for line in text.splitlines()
        if line.startswith("* ") or line.startswith("  ")
    ]
    # The last data row should be the recalled CLAUDE.md (no "*" marker);
    # everything above it is a "*"-marked never-recalled row.
    assert row_lines[-1].startswith("  ")
    assert "CLAUDE.md" in row_lines[-1]
    assert all(line.startswith("* ") for line in row_lines[:-1])


# --------------------------------------------------------------------------
# --json
# --------------------------------------------------------------------------


def test_json_output_parses_and_has_expected_keys(capsys):
    exit_code = cli.main(["scan", str(FAKE_PROJECT), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert set(payload.keys()) == {"meta", "records", "totals", "growth"}
    assert set(payload["totals"].keys()) >= {
        "total_tokens",
        "file_count",
        "never_recalled_count",
        "budget",
        "budget_exceeded",
    }
    assert isinstance(payload["records"], list)
    assert len(payload["records"]) > 0
    record = payload["records"][0]
    assert set(record.keys()) >= {
        "path",
        "rel_path",
        "kind",
        "tokens",
        "recall_count",
        "last_recalled",
        "never_recalled",
        "growth_flagged",
    }


# --------------------------------------------------------------------------
# --budget exit codes
# --------------------------------------------------------------------------


def test_budget_under_exits_zero():
    exit_code = cli.main(["scan", str(FAKE_PROJECT), "--budget", "1000000"])
    assert exit_code == 0


def test_budget_over_exits_one():
    exit_code = cli.main(["scan", str(FAKE_PROJECT), "--budget", "0"])
    assert exit_code == 1


def test_no_budget_always_exits_zero():
    exit_code = cli.main(["scan", str(FAKE_PROJECT)])
    assert exit_code == 0


# --------------------------------------------------------------------------
# --sessions / --model plumbing
# --------------------------------------------------------------------------


def test_sessions_flag_reaches_recall_join(monkeypatch, tmp_path):
    calls = []
    original_join = cli.recall_join

    def spy_join(inventory, sessions=30, projects_dir=None, project_root=None):
        calls.append(sessions)
        return original_join(
            inventory, sessions=sessions, projects_dir=projects_dir, project_root=project_root
        )

    monkeypatch.setattr(cli, "recall_join", spy_join)

    report = cli.build_report(FAKE_PROJECT, sessions=7, cache_dir=tmp_path / "cache")
    assert calls == [7]
    assert report["meta"]["sessions"] == 7


def test_model_flag_reaches_tokens_weigh_and_changes_calibration(tmp_path):
    cache_dir = tmp_path / "cache"
    newer = cli.build_report(FAKE_PROJECT, model="claude-opus-5", cache_dir=cache_dir)
    older = cli.build_report(FAKE_PROJECT, model="claude-opus-4-6", cache_dir=cache_dir)

    assert newer["meta"]["model"] == "claude-opus-5"
    assert older["meta"]["model"] == "claude-opus-4-6"
    # Different chars-per-token calibration between tokenizer families must
    # produce different totals for the same fixture.
    assert newer["totals"]["total_tokens"] != older["totals"]["total_tokens"]
    # And the per-record error bar must reflect the requested model.
    heuristic_records = [r for r in newer["records"] if r["token_method"] == "heuristic"]
    assert heuristic_records
    assert heuristic_records[0]["error_pct"] == MODEL_ERROR_PCT["claude-opus-5"]


def test_model_cli_flag_end_to_end(capsys):
    cli.main(["scan", str(FAKE_PROJECT), "--json", "--model", "claude-opus-4-6"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["meta"]["model"] == "claude-opus-4-6"


def test_sessions_cli_flag_end_to_end(capsys):
    cli.main(["scan", str(FAKE_PROJECT), "--json", "--sessions", "3"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["meta"]["sessions"] == 3


# --------------------------------------------------------------------------
# --exact graceful fallback (no network, no key)
# --------------------------------------------------------------------------


def test_exact_without_api_key_falls_back_gracefully(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    report = cli.build_report(FAKE_PROJECT, exact=True, cache_dir=tmp_path / "cache")
    assert report["records"]
    assert all(r["token_method"] == "heuristic-no-key" for r in report["records"])


# --------------------------------------------------------------------------
# growth integration: whatever the git status of the fixture, the report
# and the human rendering must never crash, and must expose growth's shape.
# --------------------------------------------------------------------------


def test_scan_growth_section_present_and_human_report_renders(tmp_path):
    report = cli.build_report(FAKE_PROJECT, cache_dir=tmp_path / "cache")
    assert set(report["growth"].keys()) >= {"is_git_repo", "files", "total", "window_days"}
    cli.format_human(report)  # must render without raising either way


def _base_report(**growth_overrides) -> dict:
    """A minimal, otherwise-valid report dict for isolated format_human()
    unit tests (independent of the real fixture's actual git history)."""
    growth = {
        "is_git_repo": False,
        "window_days": 90,
        "files": {},
        "total": {"window_ratio": None},
    }
    growth.update(growth_overrides)
    return {
        "meta": {"root": "/fake/root"},
        "records": [],
        "totals": {
            "total_tokens": 0,
            "file_count": 0,
            "never_recalled_count": 0,
            "budget": None,
            "budget_exceeded": False,
        },
        "growth": growth,
    }


def test_human_report_shows_growth_headline_when_flagged():
    report = _base_report(
        is_git_repo=True, total={"window_ratio": 3.14}, window_days=90
    )
    text = cli.format_human(report)
    assert "Force-load grew 3.1x in 90 days" in text


def test_human_report_shows_no_git_history_line_when_not_a_repo():
    report = _base_report(is_git_repo=False)
    text = cli.format_human(report)
    assert "no git history" in text
    assert "Force-load grew" not in text
