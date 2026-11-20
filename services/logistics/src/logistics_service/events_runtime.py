"""Lifespan: start the outbox relay (publishes pool.assigned) and the identity consumer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI

from farm_common.events import RabbitEventBus
from farm_common.outbox import run_relay
from logistics_service.consumers import BINDINGS, make_handler
from logistics_service.database import get_session_factory
from logistics_service.db import OutboxEvent
from logistics_service.settings import get_logistics_settings

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_logistics_settings()
    relay: asyncio.Task[None] | None = None
    bus: RabbitEventBus | None = None

    if settings.events_enabled:
        bus = RabbitEventBus(settings.rabbitmq_url, service_name="logistics")
        try:
            await bus.connect()
        except Exception:
            logger.exception("Could not connect to RabbitMQ; events will queue in outbox")
            bus = None
        if bus is not None:
            session_factory = get_session_factory()
            await bus.subscribe(BINDINGS, make_handler(session_factory))
            relay = asyncio.create_task(run_relay(session_factory, OutboxEvent, bus))

    try:
        yield
    finally:
        if relay is not None:
            relay.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await relay
        if bus is not None:
            await bus.close()
