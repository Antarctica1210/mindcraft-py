"""Deterministic player-goal parsing for task contracts and shortcuts."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .command_registry import INVENTORY_PREP_COMMANDS, NAVIGATION_ONLY_ALLOWED_COMMANDS
from .completion import CompletionSpec

DEFAULT_ALIASES_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "agent" / "intent_aliases.json"
)

ITEM_REQUEST_RE = re.compile(
    r"\b(?:get|collect|gather|fetch|bring|give|find|search|mine|eat|consume)\b|"
    r"\b(?:want|need|would\s+like)\s+(?:to\s+)?(?:eat|get|have|consume)\b|"
    r"(?:给我|拿|获取|采集|收集|找|寻找|挖|挖一点|采一点)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ItemRequest:
    item_name: str
    source_block: str | None
    quantity: int = 8


@dataclass(frozen=True)
class CraftRequest:
    item_name: str
    quantity: int = 1
    additive: bool = False


@dataclass(frozen=True)
class CookRequest:
    raw_item: str
    cooked_item: str
    quantity: int | None = None


@dataclass(frozen=True)
class IntentAliases:
    item_aliases: dict[str, tuple[str, str | None]]
    craft_aliases: dict[str, str]
    cook_aliases: dict[str, tuple[str, str]]
    chinese_numbers: dict[str, int]


def _normalize_item_alias(value: Any) -> tuple[str, str | None]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ValueError(f"invalid item alias entry: {value!r}")
    item, source = value
    return str(item), None if source is None else str(source)


def _normalize_pair(value: Any) -> tuple[str, str]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ValueError(f"invalid alias pair entry: {value!r}")
    return str(value[0]), str(value[1])


@lru_cache(maxsize=4)
def load_intent_aliases(path: str | Path = DEFAULT_ALIASES_PATH) -> IntentAliases:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return IntentAliases(
        item_aliases={
            str(alias): _normalize_item_alias(value)
            for alias, value in dict(raw.get("item_aliases", {})).items()
        },
        craft_aliases={
            str(alias): str(item)
            for alias, item in dict(raw.get("craft_aliases", {})).items()
        },
        cook_aliases={
            str(alias): _normalize_pair(value)
            for alias, value in dict(raw.get("cook_aliases", {})).items()
        },
        chinese_numbers={
            str(word): int(value)
            for word, value in dict(raw.get("chinese_numbers", {})).items()
        },
    )


def alias_in_text(alias: str, text: str) -> bool:
    if re.search(r"[\u4e00-\u9fff]", alias):
        return alias in text
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])", text))


def acceptable_items_for_target(item_name: str) -> tuple[str, ...]:
    if item_name == "raw_iron":
        return ("raw_iron", "iron_ore", "deepslate_iron_ore")
    if item_name == "coal":
        return ("coal", "coal_ore", "deepslate_coal_ore")
    return (item_name,)


def message_sounds_like_delivery_request(message: str) -> bool:
    text = (message or "").lower()
    return bool(re.search(
        r"\b(give|bring|fetch|get|collect|gather|mine|make|craft|cook)\b|"
        r"给我|拿给我|带给我|获取|采集|收集|挖|做|制作|合成|烧|烤",
        text,
    ))


def parse_item_request(message: str, aliases: IntentAliases | None = None) -> ItemRequest | None:
    aliases = aliases or load_intent_aliases()
    text = message.strip().lower()
    if not ITEM_REQUEST_RE.search(text):
        return None
    if re.search(r"\bcraft\b|制作|合成", text):
        return None

    quantity = quantity_from_item_request(text, aliases)
    for alias in sorted(aliases.item_aliases, key=len, reverse=True):
        if alias_in_text(alias, text):
            item_name, source_block = aliases.item_aliases[alias]
            return ItemRequest(item_name=item_name, source_block=source_block, quantity=quantity)
    return None


def parse_craft_request(message: str, aliases: IntentAliases | None = None) -> CraftRequest | None:
    aliases = aliases or load_intent_aliases()
    text = message.strip().lower()
    if not re.search(r"\b(craft|make|build)\b|制作|合成|做", text):
        return None

    quantity = quantity_from_craft_request(text, aliases)
    additive = bool(re.search(r"\b(more|extra|additional|another)\b|再|额外|更多", text))
    for alias in sorted(aliases.craft_aliases, key=len, reverse=True):
        if alias_in_text(alias, text):
            return CraftRequest(
                item_name=aliases.craft_aliases[alias],
                quantity=quantity,
                additive=additive,
            )
    return None


def parse_cook_request(message: str, aliases: IntentAliases | None = None) -> CookRequest | None:
    aliases = aliases or load_intent_aliases()
    text = message.strip().lower()
    if not re.search(r"\b(cook|smelt|roast|bake)\b|烤|煮|烧", text):
        return None

    quantity = optional_quantity_from_request(text, aliases)
    for alias in sorted(aliases.cook_aliases, key=len, reverse=True):
        if alias_in_text(alias, text):
            raw_item, cooked_item = aliases.cook_aliases[alias]
            return CookRequest(raw_item=raw_item, cooked_item=cooked_item, quantity=quantity)
    return None


def cook_request_is_compound(message: str) -> bool:
    if parse_cook_request(message) is None:
        return False
    text = message.strip().lower()
    return bool(
        re.search(
            r"\b("
            r"pick\s*up|pickup|grab|take|get|collect|gather|fetch|find|search|"
            r"hunt|kill|attack"
            r")\b|捡|拾取|拿|找|寻找|收集|杀|打|猎|附近|旁边|nearby",
            text,
        )
    )


def planting_request_quantity(message: str, aliases: IntentAliases | None = None) -> int | None:
    aliases = aliases or load_intent_aliases()
    text = (message or "").strip().lower()
    if not re.search(
        r"\b(plant|farm|sow|seed|wheat|field|crop)\b|种|播种|耕|小麦|种子|田",
        text,
    ):
        return None
    digit = re.search(r"(?<!\d)(\d{1,2})(?!\d)", text)
    if digit:
        return max(1, min(int(digit.group(1)), 64))
    for word, value in aliases.chinese_numbers.items():
        if word in text:
            return value
    return None


def optional_quantity_from_request(text: str, aliases: IntentAliases | None = None) -> int | None:
    aliases = aliases or load_intent_aliases()
    digit = re.search(r"\b(\d{1,2})\b", text)
    if digit:
        return max(1, min(int(digit.group(1)), 64))
    for word, value in aliases.chinese_numbers.items():
        if word in text:
            return value
    if re.search(r"\b(a|an|one)\b|一个|一块|一根", text):
        return 1
    return None


def quantity_from_craft_request(text: str, aliases: IntentAliases | None = None) -> int:
    aliases = aliases or load_intent_aliases()
    digit = re.search(r"\b(\d{1,2})\b", text)
    if digit:
        return max(1, min(int(digit.group(1)), 64))
    for word, value in aliases.chinese_numbers.items():
        if word in text:
            return value
    if re.search(r"\b(a|an|one)\b|一个|一块|一根", text):
        return 1
    return 1


def quantity_from_item_request(text: str, aliases: IntentAliases | None = None) -> int:
    aliases = aliases or load_intent_aliases()
    digit = re.search(r"\b(\d{1,2})\b", text)
    if digit:
        return max(1, min(int(digit.group(1)), 64))
    for word, value in aliases.chinese_numbers.items():
        if word in text:
            return value
    if re.search(r"\b(a|an|one)\b|一个|一块|一根|一点", text):
        return 1
    return 8


def completion_spec_from_message(message: str) -> CompletionSpec:
    """Infer the task's success boundary before the first action runs."""
    if is_navigation_only_request(message):
        return CompletionSpec(
            kind="navigation",
            terminal_commands=NAVIGATION_ONLY_ALLOWED_COMMANDS,
            requires_delivery=False,
            delivery_score=0.05,
            delivery_reason="navigation task",
        )

    cook_request = parse_cook_request(message)
    if cook_request is not None:
        return CompletionSpec(
            kind="cook_item",
            target_items=(cook_request.cooked_item,),
            acceptable_items=(cook_request.cooked_item,),
            quantity=cook_request.quantity,
            terminal_commands=frozenset({"smeltItem", "givePlayer"}),
            intermediate_commands=INVENTORY_PREP_COMMANDS | frozenset({
                "collectBlocks",
                "searchForBlock",
                "searchForEntity",
                "attack",
                "clearFurnace",
            }),
            requires_delivery=True,
            delivery_score=0.90,
            delivery_reason="cooked item requested",
        )

    craft_request = parse_craft_request(message)
    if craft_request is not None:
        return CompletionSpec(
            kind="craft_item",
            target_items=(craft_request.item_name,),
            acceptable_items=(craft_request.item_name,),
            quantity=craft_request.quantity,
            terminal_commands=frozenset({"craftRecipe", "givePlayer"}),
            intermediate_commands=INVENTORY_PREP_COMMANDS | frozenset({
                "collectBlocks",
                "searchForBlock",
                "searchForEntity",
                "attack",
                "smeltItem",
                "clearFurnace",
            }),
            requires_delivery=True,
            delivery_score=0.90,
            delivery_reason="crafted item requested",
        )

    item_request = parse_item_request(message)
    if item_request is not None:
        delivery = message_sounds_like_delivery_request(message)
        acceptable = acceptable_items_for_target(item_request.item_name)
        return CompletionSpec(
            kind="collect_item",
            target_items=(item_request.item_name,),
            acceptable_items=acceptable,
            quantity=item_request.quantity,
            terminal_commands=frozenset({
                "collectBlocks",
                "breakNearbyBlocks",
                "pickupNearbyItems",
                "givePlayer",
            }),
            intermediate_commands=INVENTORY_PREP_COMMANDS | frozenset({
                "searchForBlock",
                "searchForEntity",
                "attack",
                "smeltItem",
                "clearFurnace",
            }),
            requires_delivery=delivery,
            delivery_score=0.85 if delivery else 0.20,
            delivery_reason="collectable item requested",
        )

    planting_quantity = planting_request_quantity(message)
    if planting_quantity is not None:
        return CompletionSpec(
            kind="planting",
            quantity=planting_quantity,
            terminal_commands=frozenset({"tillAndSow"}),
            intermediate_commands=INVENTORY_PREP_COMMANDS | frozenset({
                "breakNearbyBlocks",
                "collectBlocks",
                "searchForBlock",
            }),
            requires_delivery=False,
            delivery_score=0.05,
            delivery_reason="world planting task",
        )

    if re.search(r"\b(breed|feed)\b|繁殖|喂|养", (message or "").lower()):
        return CompletionSpec(
            kind="breeding",
            terminal_commands=frozenset({"breedAnimals"}),
            intermediate_commands=INVENTORY_PREP_COMMANDS | frozenset({
                "collectBlocks",
                "searchForBlock",
                "searchForEntity",
            }),
            requires_delivery=False,
            delivery_score=0.05,
            delivery_reason="world animal task",
        )

    return CompletionSpec(
        kind="unknown",
        requires_delivery=False,
        delivery_score=0.25,
        delivery_reason="no structured target inferred",
    )


def is_navigation_only_request(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    if re.search(
        r"\b(craft|make|build|collect|gather|mine|dig|break|destroy|attack|kill|"
        r"cook|smelt|give|plant|sow|farm|breed|trade|place)\b|"
        r"制作|合成|造|收集|采集|挖|破坏|攻击|杀|烤|熔炼|给我|种|播种|耕|繁殖|交易|放置",
        text,
    ):
        return False
    return bool(re.search(
        r"\b(come\s+to\s+me|come\s+here|follow\s+me|follow\s+\w+|"
        r"go\s+to\s+\w+|go\s+to\s+player|move\s+to|stay|wait)\b|"
        r"跟我来|跟着我|跟随我|过来|来我这里|到我这里|到我这|跟随|不动|停在这|等一下|"
        r"带我去|去重生点|回重生点",
        text,
    ))
