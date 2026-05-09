"""
Centralized logging configuration.

Usage: call setup_logging() once at application startup, then use
standard logging.getLogger(__name__) in all modules.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# Matches the format used by the JS side for consistency
DEFAULT_FMT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    *,
    level: int = logging.INFO,
    log_dir: Path | None = None,
    file_fmt: str = DEFAULT_FMT,
    console_fmt: str = DEFAULT_FMT,
    date_fmt: str = DATE_FMT,
) -> None:
    """Configure root logger with console and daily-rotating file output.

    Args:
        level: Minimum log level for both handlers.
        log_dir: Directory for log files. Defaults to LOG_DIR.
        file_fmt: Format string for file output.
        console_fmt: Format string for console (stderr) output.
        date_fmt: strftime format for timestamps.
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any pre-existing handlers (avoid duplicates on re-init)
    root.handlers.clear()

    # --- File handler: one file per day ---
    directory = log_dir or LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = directory / f"{today}.log"

    file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(file_fmt, datefmt=date_fmt))
    root.addHandler(file_handler)

    # --- Console handler: stderr ---
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(console_fmt, datefmt=date_fmt))
    root.addHandler(console_handler)

    root.info("Logging to %s", log_path)


def get_logger(name: str) -> logging.Logger:
    """Convenience: return a logger for the given module name."""
    return logging.getLogger(name)
