"""Tests for the optional LM Studio dispatch gate."""
from __future__ import annotations

from llm.dispatch_gate import DispatchGate, _chat_completions_url
from settings import DispatchGateSettings


def test_dispatch_gate_disabled_without_env_like_config() -> None:
    gate = DispatchGate(DispatchGateSettings(enabled=False, base_url="", model=""))

    assert gate.configured is False


def test_chat_completions_url_accepts_lm_studio_base_url() -> None:
    assert (
        _chat_completions_url("http://127.0.0.1:1234")
        == "http://127.0.0.1:1234/v1/chat/completions"
    )
    assert (
        _chat_completions_url("http://127.0.0.1:1234/v1")
        == "http://127.0.0.1:1234/v1/chat/completions"
    )
