"""Token weights: correct token counts for context-system content.

Provides ``weigh()``, the module's sole public entry point. Two ways to get a
count:

- **Heuristic (default)** — a calibrated, offline chars-per-token estimate
  with a per-model error bar. Fast, free, no network. This is an estimate,
  not a measurement: incumbent tools that assume bytes/4 for every model can
  be wrong by up to ~35% against Claude's newer tokenizer (introduced with
  Opus 4.7), so this module calibrates the divisor per tokenizer family
  instead of using one constant for every model.
- **Exact (``exact=True``)** — one HTTP call to Anthropic's free
  ``/v1/messages/count_tokens`` endpoint via ``urllib.request`` (stdlib only,
  no SDK dependency). Token counts are model-specific, so the target model is
  always passed through. Results are cached by content hash so repeat calls
  for the same (model, text) pair never hit the network twice.

Graceful degradation: ``exact=True`` never crashes and never prompts. If
``ANTHROPIC_API_KEY`` is unset, or the API call fails for any reason (network
error, timeout, non-2xx response), ``weigh()`` silently falls back to the
heuristic and marks the result's ``method`` accordingly, with a ``warning``
key explaining why.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, Union

# --------------------------------------------------------------------------
# Configuration (module-level constants — override for calibration tuning)
# --------------------------------------------------------------------------

#: Default model used when the caller doesn't specify one. Kept in sync with
#: the model catalog at build time (2026-08); accepts any forward-compatible
#: model string, not just the ones listed below.
DEFAULT_MODEL = "claude-opus-5"

#: The count_tokens endpoint. Free, separate rate limits from Messages.
COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_VERSION = "2023-06-01"

#: Default network timeout (seconds) for the --exact API call.
DEFAULT_TIMEOUT = 5.0

#: Default cache directory, resolved relative to the current working
#: directory unless the caller passes an explicit ``cache_dir`` (e.g. the
#: CLI should pass ``<project root>/.ctx-yield-cache``). Callers running
#: from elsewhere, and all tests, should pass ``cache_dir`` explicitly.
DEFAULT_CACHE_DIR = Path(".ctx-yield-cache")

#: Per-model chars-per-token calibration factors. These are ESTIMATES, not
#: measurements — derived from field reporting that Claude's tokenizer
#: introduced with Opus 4.7 counts roughly 1x-1.35x as many tokens as the
#: older tokenizer family for the same text. A flat bytes/4 (or chars/4)
#: heuristic undercounts against the newer family by close to that margin;
#: we split the divisor by tokenizer family instead of using one constant.
#: Override / extend this dict to tune calibration without touching logic.
MODEL_CHAR_FACTORS: dict[str, float] = {
    # Newer tokenizer family (introduced with Opus 4.7): denser encoding,
    # more tokens per character than the older family.
    "claude-opus-5": 3.4,
    "claude-sonnet-5": 3.4,
    "claude-fable-5": 3.4,
    "claude-mythos-5": 3.4,
    "claude-opus-4-8": 3.4,
    "claude-opus-4-7": 3.4,
    # Older tokenizer family: the traditional chars/4 rule of thumb is
    # reasonably close for these.
    "claude-opus-4-6": 4.0,
    "claude-opus-4-5": 4.0,
    "claude-opus-4-1": 4.0,
    "claude-opus-4-0": 4.0,
    "claude-sonnet-4-6": 4.0,
    "claude-sonnet-4-5": 4.0,
    "claude-sonnet-4-0": 4.0,
    "claude-haiku-4-5": 4.0,
}

#: Per-model error bar (± percent) paired with MODEL_CHAR_FACTORS above.
#: Newer-tokenizer models get a wider band because the observed range
#: (~1x-1.35x vs the older family) is itself wide; our calibration factor
#: sits at the midpoint of that range, not at either extreme.
MODEL_ERROR_PCT: dict[str, float] = {
    "claude-opus-5": 15.0,
    "claude-sonnet-5": 15.0,
    "claude-fable-5": 15.0,
    "claude-mythos-5": 15.0,
    "claude-opus-4-8": 15.0,
    "claude-opus-4-7": 15.0,
    "claude-opus-4-6": 5.0,
    "claude-opus-4-5": 5.0,
    "claude-opus-4-1": 5.0,
    "claude-opus-4-0": 5.0,
    "claude-sonnet-4-6": 5.0,
    "claude-sonnet-4-5": 5.0,
    "claude-sonnet-4-0": 5.0,
    "claude-haiku-4-5": 5.0,
}

#: Fallback calibration for a model string not in the tables above (forward
#: compatibility with future model IDs). We assume the newer, denser
#: tokenizer family (the direction the field is moving) but widen the error
#: bar further since we have no field data for an unrecognized model.
DEFAULT_CHAR_FACTOR = 3.4
DEFAULT_ERROR_PCT = 25.0

# --------------------------------------------------------------------------
# Public interface
# --------------------------------------------------------------------------

#: Type of a text/path argument accepted by weigh(): a ``str`` is treated as
#: literal text content; a ``pathlib.Path`` (or any os.PathLike) is treated
#: as a file to read. This split is a deliberate, documented contract (see
#: interface.md) rather than a path-vs-content guess.
TextOrPath = Union[str, "os.PathLike[str]"]

#: Transport hook for the --exact HTTP call, matching the signature of
#: ``urllib.request.urlopen`` — ``(request, timeout=...) -> response``, where
#: ``response`` is a context manager exposing ``.read() -> bytes``. Tests
#: inject a fake transport here instead of hitting the network.
Transport = Callable[..., Any]


def weigh(
    text_or_path: TextOrPath,
    exact: bool = False,
    model: str = DEFAULT_MODEL,
    *,
    cache_dir: Optional[Union[str, "os.PathLike[str]"]] = None,
    api_key: Optional[str] = None,
    transport: Optional[Transport] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Weigh ``text_or_path`` in tokens for ``model``.

    Args:
        text_or_path: literal text (``str``) or a file to read
            (``pathlib.Path`` / any ``os.PathLike``).
        exact: if True, try the count_tokens API first (see fallback rules
            below); if False (default), always use the offline heuristic.
        model: target model ID. Token counts are model-specific. Accepts any
            string for forward-compatibility with future model IDs.
        cache_dir: override the exact-count cache directory (mainly for
            tests). Defaults to ``DEFAULT_CACHE_DIR`` resolved against cwd.
        api_key: override the API key instead of reading
            ``ANTHROPIC_API_KEY`` from the environment (mainly for tests).
        transport: override the HTTP transport instead of
            ``urllib.request.urlopen`` (mainly for tests — no real network
            call is ever made in this repo's test suite).
        timeout: network timeout in seconds for the exact API call.

    Returns:
        A dict with at least:
          - ``tokens`` (int): the token count (estimated or exact).
          - ``method`` (str): one of ``"exact"``, ``"heuristic"``,
            ``"heuristic-no-key"`` (exact requested, no API key available),
            or ``"heuristic-network-error"`` (exact requested, API call
            failed).
          - ``error_pct`` (float): the estimate's error bar as a ± percent;
            ``0.0`` for ``"exact"``.
        Fallback results (``method`` != ``"exact"`` while ``exact=True`` was
        requested) additionally carry a ``warning`` (str) explaining why.
        A successful exact result additionally carries ``cached`` (bool):
        whether this count came from the on-disk cache rather than a fresh
        API call.

    Never raises for API/network/auth problems — those degrade to the
    heuristic. Reading errors on ``text_or_path`` (e.g. a missing file) are
    not caught and propagate normally, since silently fabricating a count
    for unreadable input would be worse than failing loudly.
    """
    text = _resolve_text(text_or_path)

    if not exact:
        return _heuristic(text, model)

    key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        result = _heuristic(text, model)
        result["method"] = "heuristic-no-key"
        result["warning"] = (
            "ANTHROPIC_API_KEY not set; used the offline heuristic instead of --exact"
        )
        return result

    resolved_cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    cache_key = _cache_key(model, text)
    cached_tokens = _cache_get(resolved_cache_dir, cache_key)
    if cached_tokens is not None:
        return {"tokens": cached_tokens, "method": "exact", "error_pct": 0.0, "cached": True}

    try:
        tokens = _count_tokens_exact(text, model, key, transport, timeout)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: never crash on --exact
        result = _heuristic(text, model)
        result["method"] = "heuristic-network-error"
        result["warning"] = f"count_tokens API call failed ({exc!r}); used the offline heuristic"
        return result

    _cache_set(resolved_cache_dir, cache_key, tokens)
    return {"tokens": tokens, "method": "exact", "error_pct": 0.0, "cached": False}


# --------------------------------------------------------------------------
# Heuristic
# --------------------------------------------------------------------------


def _heuristic(text: str, model: str) -> dict[str, Any]:
    chars_per_token = MODEL_CHAR_FACTORS.get(model, DEFAULT_CHAR_FACTOR)
    error_pct = MODEL_ERROR_PCT.get(model, DEFAULT_ERROR_PCT)
    n_chars = len(text)
    tokens = round(n_chars / chars_per_token) if n_chars else 0
    return {"tokens": tokens, "method": "heuristic", "error_pct": error_pct}


# --------------------------------------------------------------------------
# Exact (count_tokens API)
# --------------------------------------------------------------------------


def _count_tokens_exact(
    text: str,
    model: str,
    api_key: str,
    transport: Optional[Transport],
    timeout: float,
) -> int:
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": text}]}
    ).encode("utf-8")
    request = urllib.request.Request(
        COUNT_TOKENS_URL,
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )
    opener = transport if transport is not None else urllib.request.urlopen
    with opener(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return int(payload["input_tokens"])


# --------------------------------------------------------------------------
# Content-hash cache
#
# Layout: one JSON file per (model, text) pair under cache_dir, named
# "<sha256 of model + NUL + text>.json", containing {"tokens": int,
# "model": str}. One file per key (rather than a single index file) avoids
# read-modify-write races between concurrent ctx-yield invocations.
# --------------------------------------------------------------------------


def _cache_key(model: str, text: str) -> str:
    digest = hashlib.sha256()
    digest.update(model.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.json"


def _cache_get(cache_dir: Path, key: str) -> Optional[int]:
    path = _cache_path(cache_dir, key)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
        return int(data["tokens"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Corrupt or unrecognized cache entry: treat as a miss rather than
        # crash the caller.
        return None


def _cache_set(cache_dir: Path, key: str, tokens: int) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = _cache_path(cache_dir, key)
        path.write_text(json.dumps({"tokens": tokens}), encoding="utf-8")
    except OSError:
        # Cache is a pure optimization; a write failure (e.g. read-only fs)
        # should never break a successful --exact call.
        pass


# --------------------------------------------------------------------------
# Input resolution
# --------------------------------------------------------------------------


def _resolve_text(text_or_path: TextOrPath) -> str:
    if isinstance(text_or_path, str):
        return text_or_path
    if isinstance(text_or_path, os.PathLike):
        return Path(text_or_path).read_text(encoding="utf-8", errors="replace")
    raise TypeError(
        "weigh() accepts a str (literal text) or an os.PathLike (file to read), "
        f"got {type(text_or_path)!r}"
    )
