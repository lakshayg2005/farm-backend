"""Startup/shutdown: connect to RabbitMQ, run the outbox relay and the event consumers."""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from catalog_service.consumers import BINDINGS, make_handler
from catalog_service.database import get_session_factory
from catalog_service.db import OutboxEvent
from catalog_service.settings import get_catalog_settings
from farm_common.events import RabbitEventBus
from farm_common.outbox import run_relay


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_catalog_settings()
    if not settings.events_enabled:
        yield
        return
    session_factory = get_session_factory()
    bus = RabbitEventBus(settings.rabbitmq_url, service_name="catalog")
    await bus.connect()
    await bus.subscribe(BINDINGS, make_handler(session_factory))
    relay = asyncio.create_task(run_relay(session_factory, OutboxEvent, bus))
    try:
        yield
    finally:
        relay.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay
        await bus.close()
