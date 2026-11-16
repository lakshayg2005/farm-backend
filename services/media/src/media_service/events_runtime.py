"""Startup/shutdown: object storage bucket, the arq job queue, and the outbox relay.

Photo processing itself runs in the arq worker process (media_service.worker).
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI

from farm_common.events import RabbitEventBus
from farm_common.outbox import run_relay
from media_service.database import get_session_factory
from media_service.db import OutboxEvent
from media_service.jobs import ArqQueue
from media_service.settings import get_media_settings
from media_service.storage import get_store

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_media_settings()
    queue: ArqQueue | None = None
    relay: asyncio.Task[None] | None = None
    bus: RabbitEventBus | None = None
    if settings.jobs_enabled:
        try:
            await get_store().ensure_bucket()
        except Exception:
            logger.exception("Could not reach object storage at %s", settings.s3_endpoint)
        try:
            queue = await ArqQueue.connect(settings.redis_url)
        except Exception:
            logger.exception("Could not reach Redis; photos will wait for the worker's sweeper")
        app.state.queue = queue
    if settings.events_enabled:
        bus = RabbitEventBus(settings.rabbitmq_url, service_name="media")
        await bus.connect()
        relay = asyncio.create_task(run_relay(get_session_factory(), OutboxEvent, bus))
    try:
        yield
    finally:
        if relay is not None:
            relay.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await relay
        if bus is not None:
            await bus.close()
        if queue is not None:
            await queue.close()
