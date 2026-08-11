"""Tests for ctx_yield.tokens.

No real network calls: the --exact path is exercised via an injected
``transport`` callable, never ``ANTHROPIC_API_KEY`` from a real environment
or the actual API. ``monkeypatch`` scrubs the env var in every test that
touches the exact path so CI state can't leak in either direction.
"""

from __future__ import annotations

import json

import pytest

from ctx_yield import tokens


class FakeResponse:
    """Minimal stand-in for the object urllib.request.urlopen() yields."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def make_transport(input_tokens: int, calls: list) -> object:
    """A fake transport that records calls and returns a fixed count."""

    def _transport(request, timeout=None):  # noqa: ANN001 - matches urlopen sig
        calls.append(request)
        return FakeResponse({"input_tokens": input_tokens})

    return _transport


def failing_transport(request, timeout=None):  # noqa: ANN001
    raise OSError("simulated network failure")


# --------------------------------------------------------------------------
# Heuristic
# --------------------------------------------------------------------------


def test_heuristic_returns_sane_numbers_and_error_bar():
    text = "x" * 400
    result = tokens.weigh(text, model="claude-opus-5")
    assert result["method"] == "heuristic"
    assert result["tokens"] > 0
    # chars_per_token for claude-opus-5 is 3.4 -> ~118 tokens
    assert 80 <= result["tokens"] <= 160
    assert result["error_pct"] == tokens.MODEL_ERROR_PCT["claude-opus-5"]


def test_heuristic_empty_text_is_zero_tokens():
    result = tokens.weigh("", model="claude-opus-5")
    assert result["tokens"] == 0
    assert result["method"] == "heuristic"


def test_heuristic_legacy_vs_new_tokenizer_family_differ():
    text = "some representative context content " * 20
    new_family = tokens.weigh(text, model="claude-opus-5")
    legacy_family = tokens.weigh(text, model="claude-sonnet-4-6")
    # Newer tokenizer family is denser (more tokens per char) by construction.
    assert new_family["tokens"] > legacy_family["tokens"]


def test_heuristic_unknown_model_uses_default_factor_and_wider_error():
    text = "hello world " * 10
    result = tokens.weigh(text, model="claude-some-future-model")
    assert result["method"] == "heuristic"
    assert result["error_pct"] == tokens.DEFAULT_ERROR_PCT
    expected_tokens = round(len(text) / tokens.DEFAULT_CHAR_FACTOR)
    assert result["tokens"] == expected_tokens


def test_default_model_is_defined_and_used():
    result = tokens.weigh("some text")
    assert result["method"] == "heuristic"
    # Default model should resolve to the "new" tokenizer family's error bar.
    assert result["error_pct"] == tokens.MODEL_ERROR_PCT[tokens.DEFAULT_MODEL]


# --------------------------------------------------------------------------
# Input shapes: str content vs Path
# --------------------------------------------------------------------------


def test_accepts_str_literal_content():
    result = tokens.weigh("just some literal text")
    assert result["tokens"] > 0


def test_accepts_path_reads_file(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("content read from disk", encoding="utf-8")
    result = tokens.weigh(file_path)
    expected = tokens.weigh("content read from disk")
    assert result["tokens"] == expected["tokens"]


def test_rejects_unsupported_type():
    with pytest.raises(TypeError):
        tokens.weigh(12345)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Exact path
# --------------------------------------------------------------------------


def test_exact_parses_input_tokens(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls: list = []
    transport = make_transport(42, calls)
    result = tokens.weigh(
        "some text to count",
        exact=True,
        model="claude-opus-5",
        cache_dir=tmp_path,
        api_key="sk-ant-test-key",
        transport=transport,
    )
    assert result == {"tokens": 42, "method": "exact", "error_pct": 0.0, "cached": False}
    assert len(calls) == 1


def test_exact_cache_hit_on_second_call_no_second_network_call(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls: list = []
    transport = make_transport(99, calls)

    first = tokens.weigh(
        "cache me please",
        exact=True,
        model="claude-opus-5",
        cache_dir=tmp_path,
        api_key="sk-ant-test-key",
        transport=transport,
    )
    second = tokens.weigh(
        "cache me please",
        exact=True,
        model="claude-opus-5",
        cache_dir=tmp_path,
        api_key="sk-ant-test-key",
        transport=transport,
    )

    assert first["tokens"] == 99
    assert first["cached"] is False
    assert second["tokens"] == 99
    assert second["cached"] is True
    assert len(calls) == 1  # only the first call hit the transport


def test_exact_cache_key_varies_by_model(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls: list = []
    transport = make_transport(7, calls)

    tokens.weigh(
        "same text",
        exact=True,
        model="claude-opus-5",
        cache_dir=tmp_path,
        api_key="sk-ant-test-key",
        transport=transport,
    )
    tokens.weigh(
        "same text",
        exact=True,
        model="claude-sonnet-5",
        cache_dir=tmp_path,
        api_key="sk-ant-test-key",
        transport=transport,
    )

    # Different model => different cache key => two network calls.
    assert len(calls) == 2


def test_exact_sends_verified_wire_shape(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    captured: list = []

    def transport(request, timeout=None):  # noqa: ANN001
        captured.append(request)
        return FakeResponse({"input_tokens": 5})

    tokens.weigh(
        "wire shape check",
        exact=True,
        model="claude-opus-5",
        cache_dir=tmp_path,
        api_key="sk-ant-test-key",
        transport=transport,
    )

    assert len(captured) == 1
    request = captured[0]
    assert request.full_url == "https://api.anthropic.com/v1/messages/count_tokens"
    assert request.get_header("X-api-key") == "sk-ant-test-key"
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data.decode("utf-8"))
    assert body == {
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": "wire shape check"}],
    }


# --------------------------------------------------------------------------
# Graceful fallback
# --------------------------------------------------------------------------


def test_no_key_fallback_to_heuristic(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls: list = []
    transport = make_transport(1, calls)

    result = tokens.weigh(
        "no key here",
        exact=True,
        model="claude-opus-5",
        cache_dir=tmp_path,
        transport=transport,
        # api_key intentionally omitted -> falls through to env, which is unset
    )

    assert result["method"] == "heuristic-no-key"
    assert result["tokens"] > 0
    assert result["error_pct"] == tokens.MODEL_ERROR_PCT["claude-opus-5"]
    assert "warning" in result
    assert len(calls) == 0  # never attempted the network call


def test_network_error_falls_back_to_heuristic_with_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = tokens.weigh(
        "network is down",
        exact=True,
        model="claude-opus-5",
        cache_dir=tmp_path,
        api_key="sk-ant-test-key",
        transport=failing_transport,
    )

    assert result["method"] == "heuristic-network-error"
    assert result["tokens"] > 0
    assert "warning" in result
    assert "simulated network failure" in result["warning"] or "OSError" in result["warning"]


def test_env_api_key_is_used_when_not_passed_explicitly(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    captured: list = []

    def transport(request, timeout=None):  # noqa: ANN001
        captured.append(request)
        return FakeResponse({"input_tokens": 3})

    result = tokens.weigh(
        "env key test",
        exact=True,
        model="claude-opus-5",
        cache_dir=tmp_path,
        transport=transport,
    )

    assert result["method"] == "exact"
    assert captured[0].get_header("X-api-key") == "sk-ant-from-env"
