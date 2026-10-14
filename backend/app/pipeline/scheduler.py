"""
In-process background polling for replies/bounces (§6.7, §8), so staff don't
have to remember to click "Check replies" for it to happen. Runs as an
asyncio loop inside the API process rather than a Railway cron job, because
standard cron scheduling doesn't support the sub-hour, sub-minute-adjacent
intervals this needs -- see settings.auto_tick_interval_seconds.

The manual "Check replies" button in the dashboard still works exactly as
before; this just means you don't have to click it for things to happen.
"""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.pipeline.tick import run_pipeline_tick
from app.services.gmail_service import get_gmail_client
from app.services.storage_service import get_lead_store, get_pipeline_state_store

logger = logging.getLogger("outreach.scheduler")


async def _run_one_tick() -> None:
    gmail = get_gmail_client()
    store = get_lead_store()
    state_store = get_pipeline_state_store()
    # run_pipeline_tick is synchronous and does blocking network I/O (Gmail,
    # OpenAI) -- push it to a worker thread so it doesn't stall the event
    # loop (and everyone else's API requests) while it runs.
    outcomes = await asyncio.to_thread(run_pipeline_tick, gmail, store, state_store)
    noteworthy = [o for o in outcomes if o.action not in ("waiting", "not_awaiting_reply")]
    if noteworthy:
        logger.info("auto-tick: %s", ", ".join(f"{o.lead_id[:8]}={o.action}" for o in noteworthy))


async def auto_tick_loop() -> None:
    interval = settings.auto_tick_interval_seconds
    if interval <= 0:
        logger.info("auto-tick disabled (AUTO_TICK_INTERVAL_SECONDS=0)")
        return
    logger.info("auto-tick started, polling every %ss", interval)
    while True:
        try:
            await _run_one_tick()
        except Exception:  # noqa: BLE001 -- one bad cycle must never kill the loop
            logger.exception("auto-tick cycle failed, will retry next interval")
        await asyncio.sleep(interval)
