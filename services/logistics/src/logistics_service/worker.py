"""arq worker: nightly pooling cron + on-demand pool run jobs.

    arq logistics_service.worker.WorkerSettings   # start the worker

Nightly cron fires at POOLING_CRON_HOUR:POOLING_CRON_MINUTE UTC (IMMEDIATE orders only, a safety
net). The shared-delivery batch (FLEXIBLE orders) fires daily at BATCH_HOUR_IST:BATCH_MINUTE_IST.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from farm_common.log import configure_logging
from logistics_service.pooling import fetch_ready_orders, india_today, run_pooling
from logistics_service.settings import LogisticsSettings, get_logistics_settings

logger = logging.getLogger(__name__)

RUN_POOL = "run_pool"


async def run_pool_job(
    ctx: dict[str, Any], pool_date_iso: str | None = None, mode: str | None = None
) -> int:
    """arq job: run the pooling engine for `pool_date_iso` (today if None).

    `mode` is the delivery mode to pool: IMMEDIATE, or FLEXIBLE for the shared-delivery batch
    (which clusters more widely); None pools both.

    Returns the number of pools created.
    """
    settings: LogisticsSettings = ctx["settings"]
    session_factory = ctx["session_factory"]

    pool_date = date.fromisoformat(pool_date_iso) if pool_date_iso else india_today()
    orders = await fetch_ready_orders(pool_date, settings.order_service_url, mode)
    pools = await run_pooling(
        pool_date, orders, session_factory, settings, batch=mode == "FLEXIBLE"
    )
    logger.info("Pooling (%s) for %s: %d pools created", mode or "all", pool_date, len(pools))
    return len(pools)


async def nightly_pool(ctx: dict[str, Any]) -> int:
    """Cron entry-point (02:00 India time): safety net for IMMEDIATE orders whose pooling on
    packing did not happen (a restart, an outage). FLEXIBLE orders are left for the batch. The
    container clock is UTC, where it is still the day before."""
    return await run_pool_job(ctx, india_today().isoformat(), "IMMEDIATE")


async def flexible_batch(ctx: dict[str, Any]) -> int:
    """Cron entry-point (10:00 India time by default): the shared-delivery batch. Clusters every
    waiting FLEXIBLE order due today or earlier, so each partner gets a neighbourhood."""
    return await run_pool_job(ctx, india_today().isoformat(), "FLEXIBLE")


def batch_cron_utc(hour_ist: int, minute_ist: int) -> tuple[int, int]:
    """The batch time in UTC, which is the clock arq's cron runs on (India has no daylight saving)."""
    total = (hour_ist * 60 + minute_ist - 330) % (24 * 60)
    return total // 60, total % 60


async def startup(ctx: dict[str, Any]) -> None:
    from logistics_service.database import get_session_factory

    settings = get_logistics_settings()
    configure_logging("logistics-worker", settings.log_level)
    ctx["settings"] = settings
    ctx["session_factory"] = get_session_factory()


class WorkerSettings:
    functions = [run_pool_job]  # type: ignore[assignment]
    cron_jobs = [
        cron(
            nightly_pool,
            hour=get_logistics_settings().pooling_cron_hour,
            minute=get_logistics_settings().pooling_cron_minute,
            second=0,
        ),
        cron(
            flexible_batch,
            hour=batch_cron_utc(
                get_logistics_settings().batch_hour_ist, get_logistics_settings().batch_minute_ist
            )[0],
            minute=batch_cron_utc(
                get_logistics_settings().batch_hour_ist, get_logistics_settings().batch_minute_ist
            )[1],
            second=0,
        ),
    ]  # type: ignore[assignment]
    on_startup = startup
    # Own queue, not arq's shared default: the media worker is on the same Redis.
    queue_name = "logistics:jobs"
    redis_settings = RedisSettings.from_dsn(get_logistics_settings().redis_url)
    keep_result = 600
