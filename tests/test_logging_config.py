from __future__ import annotations

import logging
from datetime import datetime, timezone

import logging_config
from logging_config import setup_logging


def test_setup_logging_uses_configured_timezone_for_log_filename(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("LOG_TIMEZONE", "Pacific/Auckland")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            value = datetime(2026, 5, 16, 12, 30, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(logging_config, "datetime", FixedDateTime)

    setup_logging(level=logging.INFO, log_dir=tmp_path)

    log_files = list(tmp_path.glob("*.log"))
    assert len(log_files) == 1
    assert log_files[0].name == "2026-05-17.log"
