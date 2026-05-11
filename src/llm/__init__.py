"""
Phase 1 — LLM model layer.

Unified interface via LiteLLM for 100+ LLM providers.
Supports LM Studio, DeepSeek, Qwen, OpenAI, Anthropic, and any
OpenAI-compatible endpoint.
"""
from .input_router import InputIntent, InputRouter, RouterDecision
from .prompter import LLMResponse, Prompter

__all__ = [
    "Prompter",
    "LLMResponse",
    "InputRouter",
    "InputIntent",
    "RouterDecision",
]
