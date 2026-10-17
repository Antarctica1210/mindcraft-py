from __future__ import annotations

import json

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration

from llm.prompter import (
    LLMOutput,
    Prompter,
    ToolCallFunction,
    ToolCallOutput,
    _LLMOutputParser,
    _log_raw_response,
)
from settings import LLMSettings


def test_parser_recovers_invalid_tool_calls() -> None:
    parser = _LLMOutputParser(model_name="test-model")
    msg = AIMessage(
        content="",
        invalid_tool_calls=[{
            "id": "call_1",
            "name": "action:craftRecipe",
            "args": '{"recipe_name": "white_bed", "num": 1}',
            "error": "raw provider arguments were not parsed by LangChain",
        }],
    )

    output = parser.parse_result([ChatGeneration(message=msg)])

    assert output.content == ""
    assert len(output.tool_calls) == 1
    tool_call = output.tool_calls[0]
    assert tool_call.id == "call_1"
    assert tool_call.function.name == "action:craftRecipe"
    assert tool_call.function.arguments == '{"recipe_name": "white_bed", "num": 1}'


def test_parser_extracts_text_from_list_content() -> None:
    parser = _LLMOutputParser(model_name="test-model")
    msg = AIMessage(content=[
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ])

    output = parser.parse_result([ChatGeneration(message=msg)])

    assert output.content == "first\nsecond"


def test_parser_uses_reasoning_content_only_without_tool_calls() -> None:
    parser = _LLMOutputParser(model_name="test-model")
    msg = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "I can answer from reasoning."},
    )

    output = parser.parse_result([ChatGeneration(message=msg)])

    assert output.content == "I can answer from reasoning."


def test_parser_does_not_use_reasoning_content_for_tool_call_response() -> None:
    parser = _LLMOutputParser(model_name="test-model")
    msg = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "hidden plan"},
        tool_calls=[{
            "id": "call_1",
            "name": "action:goToPlayer",
            "args": {"player_name": "Wilson", "closeness": 3},
        }],
    )

    output = parser.parse_result([ChatGeneration(message=msg)])

    assert output.content == ""
    assert len(output.tool_calls) == 1
    assert output.tool_calls[0].function.name == "action:goToPlayer"


def test_raw_response_log_is_jsonl(monkeypatch, tmp_path) -> None:
    import llm.prompter as prompter_module

    monkeypatch.setattr(
        prompter_module.Path,
        "resolve",
        lambda self: tmp_path / "src/llm/prompter.py",
    )

    msg = AIMessage(
        content="hello",
        response_metadata={"finish_reason": "stop", "id": "chatcmpl_test"},
    )

    _log_raw_response(
        model="test-model",
        tool_choice="none",
        attempt=1,
        raw_result=msg,
        parsed=LLMOutput(content="hello", model="test-model", finish_reason="stop"),
    )

    log_files = list((tmp_path / "logs").glob("*.llm_responses.jsonl"))
    assert len(log_files) == 1
    record = json.loads(log_files[0].read_text(encoding="utf-8").strip())
    assert record["model"] == "test-model"
    assert record["tool_choice"] == "none"
    assert record["raw_response"]["content"] == "hello"
    assert record["parsed"]["content"] == "hello"


def test_prompter_filters_unknown_tool_calls() -> None:
    prompter = object.__new__(Prompter)
    prompter._tools = [  # type: ignore[attr-defined]
        {
            "type": "function",
            "function": {"name": "action:craftRecipe"},
        }
    ]
    output = LLMOutput(
        content="",
        model="fake",
        tool_calls=[
            ToolCallOutput(
                id="bad",
                function=ToolCallFunction(name="action_crafting_plan_white_bed", arguments="{}"),
            ),
            ToolCallOutput(
                id="good",
                function=ToolCallFunction(name="action:craftRecipe", arguments="{}"),
            ),
        ],
    )

    filtered = prompter._filter_unknown_tool_calls(output)

    assert [tc.id for tc in filtered.tool_calls] == ["good"]


def test_prompter_disables_thinking_in_chat_model_payload() -> None:
    prompter = Prompter(
        LLMSettings(
            model="openai/google/gemma-4-26b-a4b",
            base_url="http://localhost:1234/v1",
            api_key="test",
            disable_thinking=True,
            reasoning_effort="none",
        )
    )

    payload = prompter._model._get_request_payload([])  # type: ignore[attr-defined]

    assert payload["reasoning_effort"] == "none"
    assert payload["extra_body"]["enable_thinking"] is False
    assert payload["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_prompter_disables_deepseek_thinking_with_official_payload() -> None:
    prompter = Prompter(
        LLMSettings(
            model="deepseek/deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key="test",
            disable_thinking=True,
            reasoning_effort="none",
        )
    )

    payload = prompter._model._get_request_payload([])  # type: ignore[attr-defined]

    assert payload["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in payload
