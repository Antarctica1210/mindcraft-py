"""Optional schema-based dispatcher for action-command routing.

The gate is intentionally advisory.  If it is not configured, times out, or
returns invalid JSON, callers should keep the existing regex/shortcut/planner
flow unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from settings import DispatchGateSettings

if TYPE_CHECKING:
    from agent.models import BotState

logger = logging.getLogger(__name__)


_ROUTES = {
    "direct_action",
    "direct_cook",
    "direct_item",
    "skill",
    "llm_planner",
    "chat",
    "ask_clarify",
}
_TASK_SHAPES = {"atomic", "compound", "ambiguous", "non_action"}


@dataclass(frozen=True)
class DispatchDecision:
    """Structured classifier output used to gate direct shortcuts."""

    route: str
    task_shape: str
    confidence: float
    reason: str = ""
    requested_outputs: list[str] = field(default_factory=list)
    required_steps: list[str] = field(default_factory=list)

    @property
    def allows_direct_shortcut(self) -> bool:
        return (
            self.task_shape == "atomic"
            and self.route in {"direct_action", "direct_cook", "direct_item", "skill"}
        )


class DispatchGate:
    """Call an OpenAI-compatible local model for one small routing decision."""

    def __init__(self, config: DispatchGateSettings):
        self._config = config

    @property
    def configured(self) -> bool:
        return self._config.configured

    @property
    def direct_confidence(self) -> float:
        return self._config.direct_confidence

    async def classify(self, message: str, state: BotState) -> DispatchDecision | None:
        if not self.configured:
            return None

        payload = self._build_payload(message, state)
        try:
            raw = await asyncio.to_thread(self._post_chat_completions, payload)
            decision = self._parse_response(raw)
        except Exception as exc:
            logger.info("DispatchGate unavailable; falling back to keyword routing: %r", exc)
            return None

        if decision is None:
            logger.info("DispatchGate returned no usable decision; falling back")
            return None

        logger.info(
            "DispatchGate decision: route=%s shape=%s confidence=%.2f reason=%s",
            decision.route,
            decision.task_shape,
            decision.confidence,
            decision.reason[:160],
        )
        return decision

    def _build_payload(self, message: str, state: BotState) -> dict[str, Any]:
        system = (
            "You are a Minecraft bot dispatch classifier. Return only JSON matching the "
            "schema. Decide whether the current player message is an atomic shortcut "
            "or a compound task that needs the full planner. Use direct_* routes only "
            "for single-step/atomic requests. If the request includes gathering, "
            "finding, killing, crafting setup, then cooking/delivery, choose "
            "llm_planner with task_shape compound. Be conservative: when unsure, "
            "choose llm_planner."
        )
        user = {
            "message": message,
            "state": _compact_state(state),
            "routes": sorted(_ROUTES),
            "rules": [
                "come/follow/stop can be direct_action when atomic",
                "cook existing raw food can be direct_cook when atomic",
                "give/get one known item can be direct_item when atomic",
                "multi-step requests must be llm_planner",
                "confidence below 0.85 should usually be llm_planner",
            ],
        }
        return {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            "temperature": 0,
            "top_p": 1,
            "max_tokens": self._config.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "dispatch_decision",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "route": {"type": "string", "enum": sorted(_ROUTES)},
                            "task_shape": {"type": "string", "enum": sorted(_TASK_SHAPES)},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "reason": {"type": "string"},
                            "requested_outputs": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "required_steps": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "route",
                            "task_shape",
                            "confidence",
                            "reason",
                            "requested_outputs",
                            "required_steps",
                        ],
                    },
                },
            },
        }

    def _post_chat_completions(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _chat_completions_url(self._config.base_url),
            data=data,
            headers={
                "Authorization": f"Bearer {self._config.api_key or 'lm-studio'}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._config.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body[:400]}") from exc

    @staticmethod
    def _parse_response(raw: dict[str, Any]) -> DispatchDecision | None:
        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        parsed = _loads_json_object(str(content))
        if not isinstance(parsed, dict):
            return None

        route = str(parsed.get("route", "")).strip()
        task_shape = str(parsed.get("task_shape", "")).strip()
        if route not in _ROUTES or task_shape not in _TASK_SHAPES:
            return None
        try:
            confidence = float(parsed.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        return DispatchDecision(
            route=route,
            task_shape=task_shape,
            confidence=confidence,
            reason=str(parsed.get("reason", ""))[:500],
            requested_outputs=[
                str(item) for item in parsed.get("requested_outputs", []) if item is not None
            ][:8],
            required_steps=[
                str(step) for step in parsed.get("required_steps", []) if step is not None
            ][:10],
        )


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _compact_state(state: BotState) -> dict[str, Any]:
    return {
        "position": {
            "x": round(state.position.x, 1),
            "y": round(state.position.y, 1),
            "z": round(state.position.z, 1),
        },
        "inventory": [
            {"name": item.name, "count": item.count}
            for item in (state.inventory or [])[:20]
        ],
        "nearby_blocks": list(state.nearby_block_types or [])[:24],
        "nearby_entities": [
            {
                "name": entity.name,
                "type": entity.entity_type,
                "distance": round(entity.distance, 1),
            }
            for entity in (state.entities or [])[:16]
        ],
    }


def _loads_json_object(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
