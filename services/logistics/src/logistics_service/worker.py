"""arq worker: nightly pooling cron + on-demand pool run jobs.

    arq logistics_service.worker.WorkerSettings   # start the worker

Nightly cron fires at POOLING_CRON_HOUR:POOLING_CRON_MINUTE UTC.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from farm_common.log import configure_logging
from logistics_service.pooling import fetch_ready_orders, run_pooling
from logistics_service.settings import LogisticsSettings, get_logistics_settings

logger = logging.getLogger(__name__)

RUN_POOL = "run_pool"


async def run_pool_job(ctx: dict[str, Any], pool_date_iso: str | None = None) -> int:
    """arq job: run the pooling engine for `pool_date_iso` (today if None).

    Returns the number of pools created.
    """
    settings: LogisticsSettings = ctx["settings"]
    session_factory = ctx["session_factory"]

    pool_date = date.fromisoformat(pool_date_iso) if pool_date_iso else date.today()
    orders = await fetch_ready_orders(pool_date, settings.order_service_url)
    pools = await run_pooling(pool_date, orders, session_factory, settings)
    logger.info("Nightly pooling for %s: %d pools created", pool_date, len(pools))
    return len(pools)


async def nightly_pool(ctx: dict[str, Any]) -> int:
    """Cron entry-point: pool today's orders."""
    return await run_pool_job(ctx, date.today().isoformat())


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
        )
    ]  # type: ignore[assignment]
    on_startup = startup
    # Own queue, not arq's shared default: the media worker is on the same Redis.
    queue_name = "logistics:jobs"
    redis_settings = RedisSettings.from_dsn(get_logistics_settings().redis_url)
    keep_result = 600
