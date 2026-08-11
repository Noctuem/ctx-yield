"""Tests for ctx_yield.corpus.inventory().

Uses only tests/fixtures/fake-project and tests/fixtures/fake-user — never
the real ~/.claude — per the module's injectable user_home contract.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ctx_yield.corpus import inventory

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_PROJECT = FIXTURES / "fake-project"
FAKE_USER = FIXTURES / "fake-user"


def _by_kind(entries: list[dict], kind: str) -> list[dict]:
    return [e for e in entries if e["kind"] == kind]


def _find_by_suffix(entries: list[dict], suffix: str) -> dict:
    matches = [e for e in entries if e["path"].endswith(suffix)]
    assert len(matches) == 1, f"expected exactly one entry ending {suffix!r}, got {matches}"
    return matches[0]


def _expect_hash(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def test_fixture_sanity():
    assert FAKE_PROJECT.is_dir()
    assert FAKE_USER.is_dir()


def test_discovers_project_claude_md_with_correct_hash():
    entries = inventory(FAKE_PROJECT)
    entry = _find_by_suffix(entries, "fake-project/CLAUDE.md")
    assert entry["kind"] == "claude-md"
    assert entry["source"] == "project"
    expected_bytes, expected_sha = _expect_hash(FAKE_PROJECT / "CLAUDE.md")
    assert entry["bytes"] == expected_bytes
    assert entry["sha256"] == expected_sha


def test_resolves_existing_import_and_skips_missing_one():
    entries = inventory(FAKE_PROJECT)
    imports = _by_kind(entries, "import")
    assert len(imports) == 1
    imported = imports[0]
    assert imported["path"].endswith("docs/imported.md")
    assert imported["source"] == "import"
    expected_bytes, expected_sha = _expect_hash(FAKE_PROJECT / "docs" / "imported.md")
    assert imported["bytes"] == expected_bytes
    assert imported["sha256"] == expected_sha

    # The missing import target must never appear, and discovery must not
    # raise for it.
    assert not any("does-not-exist" in e["path"] for e in entries)


def test_discovers_skills_commands_agents():
    entries = inventory(FAKE_PROJECT)

    skill = _find_by_suffix(entries, ".claude/skills/example-skill.md")
    assert skill["kind"] == "skill"
    assert skill["source"] == "project"

    command = _find_by_suffix(entries, ".claude/commands/example-command.md")
    assert command["kind"] == "command"
    assert command["source"] == "project"

    agent = _find_by_suffix(entries, ".claude/agents/example-agent.md")
    assert agent["kind"] == "agent"
    assert agent["source"] == "project"


def test_discovers_hook_script_referenced_from_settings_json():
    entries = inventory(FAKE_PROJECT)
    hooks = _by_kind(entries, "hook")
    assert len(hooks) == 1
    hook = hooks[0]
    assert hook["path"].endswith(".claude/hooks/log-tool-event.py")
    assert hook["source"] == "project"
    expected_bytes, expected_sha = _expect_hash(
        FAKE_PROJECT / ".claude" / "hooks" / "log-tool-event.py"
    )
    assert hook["bytes"] == expected_bytes
    assert hook["sha256"] == expected_sha


def test_discovers_agents_md():
    entries = inventory(FAKE_PROJECT)
    agents_md = _find_by_suffix(entries, "fake-project/AGENTS.md")
    assert agents_md["kind"] == "agents-md"
    assert agents_md["source"] == "project"


def test_user_level_discovery_is_off_by_default():
    """Without an explicit user_home, no user-level entries appear — this is
    what keeps the real ~/.claude untouched unless a caller opts in."""
    entries = inventory(FAKE_PROJECT)
    assert _by_kind(entries, "user-claude-md") == []
    assert _by_kind(entries, "memory") == []


def test_user_level_discovery_uses_injected_home_not_real_one():
    entries = inventory(FAKE_PROJECT, user_home=FAKE_USER)

    user_claude_md = _find_by_suffix(entries, "fake-user/.claude/CLAUDE.md")
    assert user_claude_md["kind"] == "user-claude-md"
    assert user_claude_md["source"] == "user"

    memory = _find_by_suffix(entries, "fake-user/.claude/memory/notes.md")
    assert memory["kind"] == "memory"
    assert memory["source"] == "user"

    # Every returned path stays under one of the two injected fixture roots
    # -- proof this run never touched a real home directory.
    allowed_roots = (FAKE_PROJECT.resolve().as_posix(), FAKE_USER.resolve().as_posix())
    for entry in entries:
        assert entry["path"].startswith(allowed_roots), entry["path"]


def test_nonexistent_project_root_returns_empty_list():
    entries = inventory(FIXTURES / "does-not-exist-project")
    assert entries == []


def test_settings_json_without_hooks_key_is_tolerated(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"FOO": "bar"}}), encoding="utf-8"
    )
    entries = inventory(tmp_path)
    assert _by_kind(entries, "hook") == []


def test_malformed_settings_json_does_not_raise(tmp_path: Path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not valid json", encoding="utf-8")
    entries = inventory(tmp_path)  # must not raise
    assert _by_kind(entries, "hook") == []


def test_every_entry_has_the_contract_fields():
    entries = inventory(FAKE_PROJECT, user_home=FAKE_USER)
    assert entries, "expected at least one discovered entry"
    for entry in entries:
        assert set(entry.keys()) == {"path", "kind", "bytes", "sha256", "source"}
        assert isinstance(entry["path"], str) and entry["path"]
        assert isinstance(entry["kind"], str) and entry["kind"]
        assert isinstance(entry["bytes"], int) and entry["bytes"] >= 0
        assert isinstance(entry["sha256"], str) and len(entry["sha256"]) == 64
        assert isinstance(entry["source"], str) and entry["source"]
