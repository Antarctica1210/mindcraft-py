#!/usr/bin/env python3
"""
Bot regression test runner and log analyzer.

Three modes::

    # Mock-only command-level tests (fast, no server, no LLM)
    uv run python scripts/run_bot_regression.py --mock

    # Scan a log file for known bad patterns
    uv run python scripts/run_bot_regression.py --scan-logs logs/2026-05-13.log

    # E2E with real socket server + LLM (needs Minecraft server)
    uv run python scripts/run_bot_regression.py --e2e --host 127.0.0.1

Output: JUnit-style XML written to ``test-reports/results.xml`` plus
``test-reports/log-scan.json`` when scanning.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPORT_DIR = ROOT / "test-reports"

# ── Log pattern scanner ───────────────────────────────────────────────
#
# Each pattern is a regex + severity + explanation.  When scanning a log
# file we flag every match so devs can triage regressions without reading
# the entire log.

LOG_PATTERNS: list[dict[str, Any]] = [
    {
        "id": "socket_crash",
        "pattern": re.compile(r"Socket server closed connection", re.IGNORECASE),
        "severity": "critical",
        "explanation": (
            "The TCP connection to the Mineflayer process died; "
            "bot is unusable until restarted."
        ),
    },
    {
        "id": "connection_reset",
        "pattern": re.compile(r"ConnectionResetError|Connection reset by peer", re.IGNORECASE),
        "severity": "critical",
        "explanation": (
            "The Minecraft server reset the connection; likely timeout during "
            "a long operation."
        ),
    },
    {
        "id": "gave_zero",
        "pattern": re.compile(r"Gave\s+0x"),
        "severity": "high",
        "explanation": (
            "givePlayer reported delivering 0 items; delivery logic may not "
            "have checked inventory."
        ),
    },
    {
        "id": "window_did_not_fire",
        "pattern": re.compile(r"windowOpen.*did not fire", re.IGNORECASE),
        "severity": "high",
        "explanation": (
            "A container window never opened; smelting or storage will fail."
        ),
    },
    {
        "id": "tossed_zero",
        "pattern": re.compile(r"tossed\s+0"),
        "severity": "high",
        "explanation": "Bot attempted to toss 0 items — delivery would silently do nothing.",
    },
    {
        "id": "search_range_exceeded",
        "pattern": re.compile(r"search_range\s*[:=]\s*(1[3-9]\d|[2-9]\d{2,})", re.IGNORECASE),
        "severity": "medium",
        "explanation": (
            "Search range exceeded 64 blocks; the LLM may be providing "
            "too-large ranges."
        ),
    },
    {
        "id": "three_consecutive_failures",
        "pattern": re.compile(r"(FAILED.*\n){3,}", re.IGNORECASE),
        "severity": "medium",
        "explanation": (
            "Three or more consecutive command failures; task is likely stuck "
            "in a retry loop."
        ),
    },
    {
        "id": "llm_refused_commands",
        "pattern": re.compile(r"!stop|!stfu.*blocked", re.IGNORECASE),
        "severity": "low",
        "explanation": (
            "LLM proposed a meta-command like !stop which was blocked; "
            "normal but worth noting."
        ),
    },
    {
        "id": "no_tool_warning",
        "pattern": re.compile(
            r"collectBlocks.*(?:stone|cobblestone|iron_ore|coal_ore|diamond_ore).*FAILED",
            re.IGNORECASE,
        ),
        "severity": "low",
        "explanation": (
            "collectBlocks failed; may be missing required tool "
            "(pickaxe check working as intended)."
        ),
    },
    {
        "id": "collect_blocks_timeout",
        "pattern": re.compile(
            r"collect_blocks.*stone.*(?:66|60|[5-9]\d)\s*seconds",
            re.IGNORECASE,
        ),
        "severity": "high",
        "explanation": (
            "collectBlocks took >50s; likely missing tool or stuck mining, "
            "may cause connection reset."
        ),
    },
]

CUSTOM_RULES: dict[str, dict[str, str]] = {
    "craft_furnace_with_nearby": {
        "severity": "medium",
        "explanation": (
            "Bot tried to craft a furnace when one was already nearby; "
            "wasted resources."
        ),
    },
    "task_succeeded_no_delivery": {
        "severity": "high",
        "explanation": (
            "Task marked succeeded but never called givePlayer; player did "
            "not receive requested item."
        ),
    },
    "repeated_same_command": {
        "severity": "low",
        "explanation": (
            "Same command type issued in consecutive actions; LLM may not be "
            "seeing prior results."
        ),
    },
}


def _line_number_for_offset(text: str, offset: int) -> int:
    return text[:offset].count("\n") + 1


def _add_custom_finding(
    report: dict[str, Any],
    *,
    rule: str,
    line: int,
    snippet: str,
) -> None:
    meta = CUSTOM_RULES[rule]
    severity = meta["severity"]
    report["findings"].append({
        "rule": rule,
        "severity": severity,
        "explanation": meta["explanation"],
        "line": line,
        "snippet": snippet[:200],
    })
    report["summary"][severity] += 1


def _task_sections(text: str) -> list[tuple[int, int, int, str]]:
    """Return (start_offset, end_offset, start_line, section_text) task chunks."""
    starts = list(re.finditer(r"ActiveTask start:", text, re.IGNORECASE))
    if not starts:
        return [(0, len(text), 1, text)]
    sections: list[tuple[int, int, int, str]] = []
    for idx, match in enumerate(starts):
        start = match.start()
        end = starts[idx + 1].start() if idx + 1 < len(starts) else len(text)
        sections.append((start, end, _line_number_for_offset(text, start), text[start:end]))
    return sections


def _scan_task_level_rules(text: str, report: dict[str, Any]) -> None:
    for _start, _end, start_line, section in _task_sections(text):
        if re.search(r"ActiveTask finish:.*status=succeeded", section, re.IGNORECASE):
            has_delivery = re.search(
                r"\b(?:givePlayer|give_to_player)\b|Gave\s+[1-9]\d*x",
                section,
                re.IGNORECASE,
            )
            expects_delivery = re.search(
                r"\b(?:cook|smelt|craft|collect|give|get|bring|find|mine)\b|"
                r"烤|煮|烧|给我|采集|收集|寻找|挖",
                section,
                re.IGNORECASE,
            )
            if expects_delivery and not has_delivery:
                finish = re.search(
                    r"ActiveTask finish:.*status=succeeded.*",
                    section,
                    re.IGNORECASE,
                )
                snippet = finish.group(0) if finish else "ActiveTask finish: status=succeeded"
                line = start_line + section[: finish.start()].count("\n") if finish else start_line
                _add_custom_finding(
                    report,
                    rule="task_succeeded_no_delivery",
                    line=line,
                    snippet=snippet,
                )

        if re.search(r"SKIPPED:\s*furnace is already nearby", section, re.IGNORECASE):
            continue
        found_furnace = re.search(
            r"(?:Found|search_for_block|searchForBlock).*furnace.*(?:nearby|goal reached|arrived)",
            section,
            re.IGNORECASE,
        )
        crafted_furnace = re.search(
            r"(?:craftRecipe|craft_recipe|Crafted).*furnace",
            section,
            re.IGNORECASE,
        )
        if found_furnace and crafted_furnace:
            _add_custom_finding(
                report,
                rule="craft_furnace_with_nearby",
                line=start_line + section[: crafted_furnace.start()].count("\n"),
                snippet=crafted_furnace.group(0),
            )


def _scan_repeated_commands(text: str, report: dict[str, Any]) -> None:
    command_rows: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        match = re.search(r"ActionExecutor start: command=([A-Za-z0-9_]+)", line)
        if match:
            command_rows.append((line_no, match.group(1), line))
    for previous, current in zip(command_rows, command_rows[1:]):
        if previous[1] == current[1]:
            _add_custom_finding(
                report,
                rule="repeated_same_command",
                line=current[0],
                snippet=f"{previous[2]}\n{current[2]}",
            )


def scan_log(log_path: str | Path) -> dict[str, Any]:
    """Scan a log file for known bad patterns. Returns a report dict."""
    path = Path(log_path)
    if not path.exists():
        return {"error": f"Log file not found: {path}"}

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    report: dict[str, Any] = {
        "file": str(path),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "total_lines": len(lines),
        "findings": [],
        "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    }

    for pat in LOG_PATTERNS:
        for m in pat["pattern"].finditer(text):
            line_no = _line_number_for_offset(text, m.start())
            finding = {
                "rule": pat["id"],
                "severity": pat["severity"],
                "explanation": pat["explanation"],
                "line": line_no,
                "snippet": m.group(0)[:200],
            }
            report["findings"].append(finding)
            report["summary"][pat["severity"]] += 1

    _scan_task_level_rules(text, report)
    _scan_repeated_commands(text, report)
    report["findings"].sort(key=lambda f: f["line"])
    return report


def scan_logs_all(log_dir: str | Path) -> dict[str, Any]:
    """Scan all .log files in a directory."""
    root = Path(log_dir)
    if not root.is_dir():
        return scan_log(root)
    results: dict[str, Any] = {"files": []}
    for log_file in sorted(root.glob("*.log")):
        results["files"].append(scan_log(log_file))
    return results


# ── Report helpers ────────────────────────────────────────────────────


def print_scan_report(report: dict[str, Any], *, limit: int = 15) -> None:
    """Pretty-print a log scan report."""
    if "files" in report:
        for f in report["files"]:
            print_scan_report(f, limit=limit)
        return

    if "error" in report:
        print(f"  Error: {report['error']}")
        return

    s = report["summary"]
    print(f"\n  {report['file']}  ({report['total_lines']} lines)")
    print(f"    critical={s['critical']} high={s['high']} medium={s['medium']} low={s['low']}")

    for i, f in enumerate(report.get("findings", [])):
        if i >= limit:
            print(f"    ... and {len(report['findings']) - limit} more")
            break
        sev = {"critical": "CRIT", "high": "HIGH", "medium": "MED ", "low": "LOW "}[f["severity"]]
        print(f"    L{f['line']:>6} [{sev}] {f['rule']}: {f['explanation']}")
        print(f"              {f['snippet'][:120]}")


# ── Pytest runner ─────────────────────────────────────────────────────


def run_mock_tests(*, verbose: bool = False, filter_expr: str = "") -> int:
    """Run command-level regression tests against SmartBridge (no server)."""
    report_dir = REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)

    args = [
        sys.executable, "-m", "pytest",
        str(ROOT / "tests" / "test_bot_regression.py"),
        "-v" if verbose else "-q",
        "--tb=short",
        "--color=yes",
        f"--junitxml={report_dir / 'results.xml'}",
    ]
    if filter_expr:
        args.extend(["-k", filter_expr])

    print(f"[run] mock tests: {' '.join(args[1:])}")
    return subprocess.run(args, cwd=str(ROOT)).returncode


def run_e2e_tests(*, host: str = "127.0.0.1", port: int = 55916,
                  username: str = "test_bot", message: str = "",
                  no_llm: bool = False) -> int:
    """Run the E2E test against a real Minecraft server."""
    args = [
        sys.executable,
        str(ROOT / "tests" / "test_e2e.py"),
        "--host", host,
        "--port", str(port),
        "--username", username,
    ]
    if no_llm:
        args.append("--no-llm")
    if message:
        args.extend(["--message", message])

    print(f"[run] e2e test: {' '.join(args[1:])}")
    return subprocess.run(args, cwd=str(ROOT)).returncode


# ── Main ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bot regression test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python scripts/run_bot_regression.py --mock
  uv run python scripts/run_bot_regression.py --mock -k "give"
  uv run python scripts/run_bot_regression.py --scan-logs logs/2026-05-13.log
  uv run python scripts/run_bot_regression.py --scan-logs logs/
  uv run python scripts/run_bot_regression.py --e2e --host 127.0.0.1
        """,
    )
    parser.add_argument(
        "--mock", action="store_true",
        help="Run command-level tests against SmartBridge (fast, no server)",
    )
    parser.add_argument(
        "--e2e", action="store_true",
        help="Run E2E test against a real Minecraft server",
    )
    parser.add_argument(
        "--scan-logs", metavar="PATH",
        help="Scan a log file or directory for known bad patterns",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Minecraft server host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=55916,
        help="Minecraft server port (default: 55916)",
    )
    parser.add_argument(
        "--username", default="test_bot",
        help="Bot username for E2E (default: test_bot)",
    )
    parser.add_argument(
        "-k", "--filter", dest="filter_expr", default="",
        help="Pytest filter expression, e.g. 'B01' or 'give or search'",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose pytest output",
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Disable LLM in E2E tests",
    )
    parser.add_argument(
        "--message", default="",
        help="Custom test message for E2E (default: preset message)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Run mock tests then scan the latest log file",
    )

    args = parser.parse_args()
    exit_code = 0

    if args.mock or args.all:
        exit_code |= run_mock_tests(
            verbose=args.verbose,
            filter_expr=args.filter_expr,
        )

    if args.scan_logs:
        report = scan_logs_all(args.scan_logs)
        print_scan_report(report)
        # Write JSON report
        report_dir = REPORT_DIR
        report_dir.mkdir(parents=True, exist_ok=True)
        scan_path = report_dir / "log-scan.json"
        scan_path.write_text(json.dumps(report, indent=2, default=str))
        print(f"\n  Full scan report → {scan_path}")

    if args.e2e:
        exit_code |= run_e2e_tests(
            host=args.host,
            port=args.port,
            username=args.username,
            message=args.message,
            no_llm=args.no_llm,
        )

    if args.all and not args.scan_logs:
        # Scan latest log after mock tests
        log_dir = ROOT / "logs"
        if log_dir.is_dir():
            latest = sorted(log_dir.glob("*.log"))[-1]
            print(f"\n[scan] Latest log: {latest}")
            report = scan_log(latest)
            print_scan_report(report, limit=10)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
