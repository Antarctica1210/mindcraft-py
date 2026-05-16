"""
Phase 1 — LLM model layer.

Unified interface via LangChain ChatOpenAI for OpenAI-compatible
LLM providers. Supports LM Studio, DeepSeek, Qwen, OpenAI, and any
OpenAI-compatible endpoint through the chain pattern:

    prompt | model | parser

Pydantic-typed I/O: LLMInput → Prompter.send_request() → LLMOutput.
"""
from .dispatch_gate import DispatchDecision, DispatchGate
from .example_store import Example, ExampleStore
from .input_router import InputIntent, InputRouter, PlanningStrategy, RouterDecision
from .prompter import LLMInput, LLMOutput, Prompter

__all__ = [
    "Prompter",
    "LLMInput",
    "LLMOutput",
    "InputRouter",
    "InputIntent",
    "PlanningStrategy",
    "RouterDecision",
    "DispatchDecision",
    "DispatchGate",
    "Example",
    "ExampleStore",
]
