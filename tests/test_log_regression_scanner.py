from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_regression_runner() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_bot_regression.py"
    spec = importlib.util.spec_from_file_location("run_bot_regression", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan_log = _load_regression_runner().scan_log


def _scan_text(tmp_path: Path, lines: list[str]) -> set[str]:
    log_path = tmp_path / "sample.log"
    log_path.write_text("\n".join(lines), encoding="utf-8")
    report = scan_log(log_path)
    return {finding["rule"] for finding in report["findings"]}


def test_scanner_detects_success_without_delivery(tmp_path: Path) -> None:
    rules = _scan_text(
        tmp_path,
        [
            "2026-05-13 [agent.core] INFO: ActiveTask start: id=1 "
            "requester=Wilson message='cook some pork'",
            "2026-05-13 [agent.core] INFO: Bot action step result: "
            "command=smeltItem result=Smelted 2x porkchop.",
            "2026-05-13 [agent.core] INFO: ActiveTask finish: id=1 "
            "status=succeeded attempts=1 failures=0 summary=Cooked pork.",
        ],
    )

    assert "task_succeeded_no_delivery" in rules


def test_scanner_accepts_success_with_positive_delivery(tmp_path: Path) -> None:
    rules = _scan_text(
        tmp_path,
        [
            "2026-05-13 [agent.core] INFO: ActiveTask start: id=1 "
            "requester=Wilson message='cook some pork'",
            "2026-05-13 [agent.core] INFO: Bot action step start: "
            "command=givePlayer kwargs={'item_name': 'cooked_porkchop', 'num': 2}",
            "2026-05-13 [agent.core] INFO: Bot action step result: "
            "command=givePlayer result=Gave 2x cooked_porkchop to Wilson.",
            "2026-05-13 [agent.core] INFO: ActiveTask finish: id=1 "
            "status=succeeded attempts=2 failures=0 summary=Cooked pork.",
        ],
    )

    assert "task_succeeded_no_delivery" not in rules


def test_scanner_detects_crafting_furnace_after_finding_one(tmp_path: Path) -> None:
    rules = _scan_text(
        tmp_path,
        [
            "2026-05-13 [agent.core] INFO: ActiveTask start: id=1 "
            "requester=Wilson message='cook some pork'",
            "2026-05-13 [agent.core] INFO: ActionExecutor done: "
            "command=searchForBlock result=Found furnace nearby.",
            "2026-05-13 [agent.core] INFO: ActionExecutor done: "
            "command=craftRecipe result=Crafted 1x furnace.",
            "2026-05-13 [agent.core] INFO: ActiveTask finish: id=1 "
            "status=failed attempts=2 failures=0 summary=Wasted furnace.",
        ],
    )

    assert "craft_furnace_with_nearby" in rules


def test_scanner_accepts_skipped_furnace_craft(tmp_path: Path) -> None:
    rules = _scan_text(
        tmp_path,
        [
            "2026-05-13 [agent.core] INFO: ActiveTask start: id=1 "
            "requester=Wilson message='cook some pork'",
            "2026-05-13 [agent.core] INFO: ActionExecutor done: "
            "command=searchForBlock result=Found furnace nearby.",
            "2026-05-13 [agent.core] INFO: Bot action step result: "
            "command=craftRecipe result=SKIPPED: furnace is already nearby.",
            "2026-05-13 [agent.core] INFO: ActiveTask finish: id=1 "
            "status=failed attempts=2 failures=0 summary=Skipped.",
        ],
    )

    assert "craft_furnace_with_nearby" not in rules


def test_scanner_detects_repeated_action_command(tmp_path: Path) -> None:
    rules = _scan_text(
        tmp_path,
        [
            "2026-05-13 [agent.executor] INFO: ActionExecutor start: "
            "command=searchForBlock args=['furnace'] handler=_search_for_block",
            "2026-05-13 [agent.executor] INFO: ActionExecutor start: "
            "command=searchForBlock args=['furnace'] handler=_search_for_block",
        ],
    )

    assert "repeated_same_command" in rules
