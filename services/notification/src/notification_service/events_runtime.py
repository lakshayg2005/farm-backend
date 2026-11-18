"""Notification event bus lifecycle."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI

from notification_service.settings import get_notification_settings

logger = logging.getLogger(__name__)

SUBSCRIBED_TOPICS = [
    "order.*",
    "payment.*",
    "reservation.*",
    "harvest.*",
    "quality.*",
    "pool.*",
    "delivery.*",
]


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_notification_settings()
    if not settings.events_enabled:
        yield
        return

    from farm_common.events import RabbitEventBus
    from notification_service.consumers import make_event_handler
    from notification_service.database import get_session_factory

    session_factory = get_session_factory()
    assert session_factory is not None
    bus = RabbitEventBus(settings.rabbitmq_url, service_name="notification")

    try:
        await bus.connect()
        handler = make_event_handler(session_factory, settings)
        await bus.subscribe(SUBSCRIBED_TOPICS, handler)
        logger.info("Notification event consumer started.")
    except Exception as exc:
        logger.warning("Could not connect to RabbitMQ (%s); running without event bus", exc)
        yield
        return

    try:
        yield
    finally:
        await bus.close()
