"""Small local scheduler; public production remains externally scheduled."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from gridbrief.config import get_settings
from gridbrief.operations import refresh_all

LOGGER = logging.getLogger(__name__)


def run_scheduler(*, once: bool = False, interval_minutes: int = 60) -> None:
    settings = get_settings()
    if not settings.automatic_refresh and not once:
        raise RuntimeError("Local scheduling is disabled; external automation owns refreshes.")
    if interval_minutes < 1:
        raise ValueError("interval_minutes must be at least 1")
    last_edition_date = None
    while True:
        LOGGER.info("Starting scheduled GridBrief refresh at %s", datetime.now(UTC).isoformat())
        try:
            today = datetime.now(UTC).date()
            generate = last_edition_date != today
            refresh_all(hours=24, generate=generate)
            if generate:
                last_edition_date = today
        except Exception:
            LOGGER.exception("Scheduled GridBrief refresh failed")
        if once:
            return
        time.sleep(interval_minutes * 60)
