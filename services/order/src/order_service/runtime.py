"""Background work started with the app: event consumers and the outbox relay."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from farm_common.events import Event, RabbitEventBus
from farm_common.outbox import run_relay
from order_service import events as ev
from order_service.db import OutboxEvent
from order_service.deps import get_session_factory
from order_service.service import handle_event
from order_service.settings import get_order_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_order_settings()
    if not settings.events_enabled:
        logger.info("EVENTS_ENABLED is false: not connecting to RabbitMQ")
        yield
        return

    session_factory = get_session_factory()
    bus = RabbitEventBus(settings.rabbitmq_url, "order")
    await bus.connect()

    async def on_event(event: Event) -> None:
        await handle_event(session_factory, event)

    await bus.subscribe(ev.CONSUMED, on_event)
    relay = asyncio.create_task(run_relay(session_factory, OutboxEvent, bus))
    try:
        yield
    finally:
        relay.cancel()
        await asyncio.gather(relay, return_exceptions=True)
        await bus.close()
