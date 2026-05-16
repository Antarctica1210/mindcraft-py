"""
Centralized logging configuration.

Usage: call setup_logging() once at application startup, then use
standard logging.getLogger(__name__) in all modules.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
DEFAULT_LOG_TIMEZONE = "Pacific/Auckland"

# Matches the format used by the JS side for consistency
DEFAULT_FMT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"


def log_timezone() -> ZoneInfo:
    """Return the timezone used for human-facing log timestamps and filenames."""
    name = os.getenv("LOG_TIMEZONE", DEFAULT_LOG_TIMEZONE).strip() or DEFAULT_LOG_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_LOG_TIMEZONE)


def log_date() -> str:
    """Current log date in the configured log timezone."""
    return datetime.now(log_timezone()).strftime("%Y-%m-%d")


class ZonedFormatter(logging.Formatter):
    """Logging formatter that renders asctime in LOG_TIMEZONE."""

    def formatTime(  # noqa: N802 - stdlib logging override
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        dt = datetime.fromtimestamp(record.created, tz=log_timezone())
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="milliseconds")


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

    log_path = directory / f"{log_date()}.log"

    file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(ZonedFormatter(file_fmt, datefmt=date_fmt))
    root.addHandler(file_handler)

    # --- Console handler: stderr ---
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(ZonedFormatter(console_fmt, datefmt=date_fmt))
    root.addHandler(console_handler)

    root.info("Logging to %s", log_path)


def get_logger(name: str) -> logging.Logger:
    """Convenience: return a logger for the given module name."""
    return logging.getLogger(name)
