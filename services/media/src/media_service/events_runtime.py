"""Startup/shutdown: make sure the bucket exists, run the outbox relay and the photo worker."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI

from farm_common.events import RabbitEventBus
from farm_common.outbox import run_relay
from media_service.database import get_session_factory
from media_service.db import OutboxEvent
from media_service.geo import get_resolver
from media_service.settings import get_media_settings
from media_service.storage import get_store
from media_service.worker import run_worker

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_media_settings()
    session_factory = get_session_factory()
    tasks: list[asyncio.Task[None]] = []
    bus: RabbitEventBus | None = None
    if settings.worker_enabled:
        store = get_store()
        try:
            await store.ensure_bucket()
        except Exception:
            logger.exception("Could not reach object storage at %s", settings.s3_endpoint)
        tasks.append(
            asyncio.create_task(run_worker(session_factory, store, get_resolver(), settings))
        )
    if settings.events_enabled:
        bus = RabbitEventBus(settings.rabbitmq_url, service_name="media")
        await bus.connect()
        tasks.append(asyncio.create_task(run_relay(session_factory, OutboxEvent, bus)))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if bus is not None:
            await bus.close()
