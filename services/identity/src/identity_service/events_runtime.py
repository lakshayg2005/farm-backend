"""Startup/shutdown: connect to RabbitMQ and run the outbox relay in the background."""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from farm_common.events import RabbitEventBus
from farm_common.outbox import run_relay
from identity_service.database import get_session_factory
from identity_service.db import OutboxEvent
from identity_service.settings import get_identity_settings


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_identity_settings()
    if not settings.events_enabled:
        yield
        return
    bus = RabbitEventBus(settings.rabbitmq_url, service_name="identity")
    await bus.connect()
    relay = asyncio.create_task(run_relay(get_session_factory(), OutboxEvent, bus))
    try:
        yield
    finally:
        relay.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay
        await bus.close()
