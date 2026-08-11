"""Context-system inventory.

Discovers every file a coding agent force-loads for a project: the project
``CLAUDE.md`` and its ``@``-imports, ``.claude/skills|commands|agents``,
hook scripts referenced from ``.claude/settings.json``, ``AGENTS.md``, and
(optionally, injectably) the user-level ``~/.claude/CLAUDE.md`` + memory
directory.

Public interface: :func:`inventory`. See ``interface.md`` (repo root,
gitignored coordination doc) for the realized field-by-field contract; the
shape is also fully pinned down by ``tests/test_corpus.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterator

# A whole-line "@path/to/file.md" is a memory import (project or user
# CLAUDE.md). Relative paths resolve against the directory of the file that
# contains the import line; absolute paths (including Windows drive forms
# like "C:/Users/...") are used as-is.
_IMPORT_LINE_RE = re.compile(r"^@(\S+)$")

# A hook "command" string in settings.json may be an arbitrary shell
# invocation ("python .claude/hooks/foo.py --flag"); pull out tokens that
# look like a script path by extension rather than trying to parse shell
# syntax.
_HOOK_SCRIPT_RE = re.compile(
    r'[^\s"\']+\.(?:py|sh|bash|ps1|js|mjs|cjs|bat|cmd)\b'
)

# .claude/<subdir> name -> kind label for files found under it.
_CLAUDE_SUBDIR_KINDS = {
    "skills": "skill",
    "commands": "command",
    "agents": "agent",
}


def _hash_file(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def _make_entry(path: Path, kind: str, source: str) -> dict:
    size, digest = _hash_file(path)
    return {
        "path": path.resolve().as_posix(),
        "kind": kind,
        "bytes": size,
        "sha256": digest,
        "source": source,
    }


def _resolve_import_target(raw: str, base_dir: Path) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return base_dir / candidate


def _find_imports(claude_md: Path) -> Iterator[Path]:
    """Yield resolved paths for each ``@``-import line in ``claude_md`` that
    actually exists on disk. Missing import targets are silently skipped —
    the spec calls for tolerating them, not erroring."""
    try:
        text = claude_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    base_dir = claude_md.parent
    for line in text.splitlines():
        match = _IMPORT_LINE_RE.match(line.strip())
        if not match:
            continue
        target = _resolve_import_target(match.group(1), base_dir)
        if target.is_file():
            yield target


def _walk_dir_kind(dir_path: Path, kind: str, source: str) -> Iterator[dict]:
    if not dir_path.is_dir():
        return
    for file_path in sorted(dir_path.rglob("*")):
        if file_path.is_file():
            yield _make_entry(file_path, kind, source)


def _iter_hook_commands(node) -> Iterator[str]:
    """Walk a parsed settings.json ``hooks`` value, yielding every
    ``command`` string found at any depth (matcher lists, hook lists, etc.)."""
    if isinstance(node, dict):
        command = node.get("command")
        if isinstance(command, str):
            yield command
        for value in node.values():
            yield from _iter_hook_commands(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_hook_commands(item)


def _find_hook_scripts(settings_json: Path, root: Path) -> Iterator[Path]:
    try:
        data = json.loads(settings_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return

    seen: set[str] = set()
    for command in _iter_hook_commands(hooks):
        for match in _HOOK_SCRIPT_RE.finditer(command):
            token = match.group(0)
            candidate = Path(token)
            if not candidate.is_absolute():
                candidate = root / token
            if candidate.is_file():
                key = candidate.resolve().as_posix()
                if key not in seen:
                    seen.add(key)
                    yield candidate


def inventory(root: str | Path, user_home: str | Path | None = None) -> list[dict]:
    """Discover the context system for a target project.

    Args:
        root: project directory to scan (contains ``CLAUDE.md``,
            ``.claude/``, ``AGENTS.md``).
        user_home: home directory standing in for ``~`` for user-level
            discovery (``<user_home>/.claude/CLAUDE.md`` and
            ``<user_home>/.claude/memory/``). Defaults to ``None``, which
            skips user-level discovery entirely — callers that want real
            ``~/.claude`` coverage must pass ``Path.home()`` explicitly.
            This keeps discovery injectable so tests never touch the real
            home directory.

    Returns:
        A list of dicts, each shaped
        ``{"path", "kind", "bytes", "sha256", "source"}``. See module
        docstring / interface.md for the kind and source vocabularies.
    """
    root = Path(root)
    entries: list[dict] = []
    seen_paths: set[str] = set()

    def add(path: Path, kind: str, source: str) -> None:
        if not path.is_file():
            return
        key = path.resolve().as_posix()
        if key in seen_paths:
            return
        seen_paths.add(key)
        entries.append(_make_entry(path, kind, source))

    def add_all(found: Iterator[dict]) -> None:
        for entry in found:
            key = entry["path"]
            if key not in seen_paths:
                seen_paths.add(key)
                entries.append(entry)

    # Project CLAUDE.md + its @-imports.
    project_claude_md = root / "CLAUDE.md"
    if project_claude_md.is_file():
        add(project_claude_md, "claude-md", "project")
        for target in _find_imports(project_claude_md):
            add(target, "import", "import")

    # .claude/{skills,commands,agents}
    claude_dir = root / ".claude"
    for subdir, kind in _CLAUDE_SUBDIR_KINDS.items():
        add_all(_walk_dir_kind(claude_dir / subdir, kind, "project"))

    # .claude/settings.json hook scripts.
    settings_json = claude_dir / "settings.json"
    if settings_json.is_file():
        for script in _find_hook_scripts(settings_json, root):
            add(script, "hook", "project")

    # AGENTS.md — generic bridge file.
    agents_md = root / "AGENTS.md"
    if agents_md.is_file():
        add(agents_md, "agents-md", "project")

    # User-level discovery: injectable, off by default (see docstring).
    if user_home is not None:
        user_home = Path(user_home)
        user_claude_dir = user_home / ".claude"

        user_claude_md = user_claude_dir / "CLAUDE.md"
        if user_claude_md.is_file():
            add(user_claude_md, "user-claude-md", "user")
            for target in _find_imports(user_claude_md):
                add(target, "import", "import")

        add_all(_walk_dir_kind(user_claude_dir / "memory", "memory", "user"))

    return entries
