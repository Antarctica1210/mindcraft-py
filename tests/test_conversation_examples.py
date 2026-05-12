"""Conversation example corpus retrieval tests."""
from __future__ import annotations

from llm.example_store import ExampleStore


def _selected_text(query: str) -> str:
    store = ExampleStore.from_config("config/conversation_examples.json")
    store.load()
    selected = store.select(query, limit=1)
    return "\n".join(turn.content for example in selected for turn in example)


def test_follow_me_selects_continuous_follow_example() -> None:
    text = _selected_text("follow me")

    assert "!followPlayer" in text
    assert "!goToPlayer" not in text


def test_come_to_me_selects_one_shot_navigation_example() -> None:
    text = _selected_text("come to me")

    assert "!goToPlayer" in text
    assert "!followPlayer" not in text


def test_stop_following_selects_stop_example() -> None:
    text = _selected_text("stop following me")

    assert "!stop" in text
    assert "Stopped following" in text


def test_common_action_examples_are_retrievable() -> None:
    expectations = {
        "place a crafting table": "!placeHere",
        "smelt raw iron": "!smeltItem",
        "give me 3 oak planks": "!givePlayer",
        "kill the zombie": "!attack",
        "put logs in chest": "!putInChest",
    }

    for query, command in expectations.items():
        assert command in _selected_text(query)
