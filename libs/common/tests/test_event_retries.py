"""Retry / dead-letter decisions of RabbitEventBus, without a RabbitMQ broker."""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from farm_common.events import (
    DEFAULT_RETRY_DELAYS,
    RETRY_HEADER,
    Event,
    RabbitEventBus,
    RetryDecision,
    decide_retry,
    retries_from_headers,
    retry_delay,
    retry_queue_name,
)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (None, 0),
        ({}, 0),
        ({RETRY_HEADER: 2}, 2),
        ({RETRY_HEADER: "2"}, 0),
        ({RETRY_HEADER: True}, 0),
        ({RETRY_HEADER: -1}, 0),
    ],
)
def test_retries_from_headers(headers: dict[str, Any] | None, expected: int) -> None:
    assert retries_from_headers(headers) == expected


def test_retry_delay_backs_off_and_caps() -> None:
    delays = (1.0, 4.0, 15.0)
    assert [retry_delay(n, delays) for n in range(1, 6)] == [1.0, 4.0, 15.0, 15.0, 15.0]
    assert retry_delay(1, ()) == 0.0


def test_decide_retry_retries_until_max_then_dead_letters() -> None:
    delays = DEFAULT_RETRY_DELAYS
    assert decide_retry(0, 3, delays) == RetryDecision("retry", 1, 1.0)
    assert decide_retry(1, 3, delays) == RetryDecision("retry", 2, 4.0)
    assert decide_retry(2, 3, delays) == RetryDecision("retry", 3, 15.0)
    assert decide_retry(3, 3, delays) == RetryDecision("dead_letter", 3)
    assert decide_retry(0, 0, delays).action == "dead_letter"


def test_retry_queue_name_has_delay_in_it() -> None:
    assert retry_queue_name("order.main", 1.0) == "order.main.retry.1000ms"
    assert retry_queue_name("order.main", 0.25) == "order.main.retry.250ms"


def test_negative_delay_rejected() -> None:
    with pytest.raises(ValueError):
        RabbitEventBus("amqp://x", "svc", retry_delays=(1.0, -1.0))


# ---------------------------------------------------------------------------
# on_message wiring, with a mocked channel
# ---------------------------------------------------------------------------


async def _subscribe(bus: RabbitEventBus) -> tuple[Any, MagicMock]:
    """Subscribe a always-failing handler; return the consumer callback and the channel mock."""
    queue = MagicMock()
    queue.bind = AsyncMock()
    queue.consume = AsyncMock()
    channel = MagicMock()
    channel.declare_queue = AsyncMock(return_value=queue)
    channel.default_exchange.publish = AsyncMock()
    bus._channel = channel
    bus._exchange = MagicMock()

    async def failing(_event: Event) -> None:
        raise RuntimeError("db is down")

    await bus.subscribe(["order.*"], failing)
    return queue.consume.await_args.args[0], channel


def _message(retries: int) -> MagicMock:
    event = Event(type="order.placed", producer="test")
    message = MagicMock()
    message.body = event.model_dump_json().encode()
    message.headers = {RETRY_HEADER: retries}
    message.ack = AsyncMock()
    message.reject = AsyncMock()
    return message


async def test_subscribe_declares_delay_queues() -> None:
    bus = RabbitEventBus("amqp://x", "order", max_retries=3, retry_delays=(1.0, 4.0))
    _, channel = await _subscribe(bus)
    declared = {c.args[0]: c.kwargs.get("arguments") for c in channel.declare_queue.await_args_list}
    assert set(declared) == {
        "order.main",
        "order.main.dlq",
        "order.main.retry.1000ms",
        "order.main.retry.4000ms",
    }
    assert declared["order.main.retry.4000ms"] == {
        "x-message-ttl": 4000,
        "x-dead-letter-exchange": "",
        "x-dead-letter-routing-key": "order.main",
    }


async def test_failed_handler_logs_exception_and_retries_via_delay_queue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = RabbitEventBus("amqp://x", "order", retry_delays=(1.0, 4.0, 15.0))
    on_message, channel = await _subscribe(bus)
    message = _message(retries=1)

    with caplog.at_level(logging.WARNING, logger="farm_common.events"):
        await on_message(message)

    message.ack.assert_awaited_once()
    message.reject.assert_not_awaited()
    published, kwargs = channel.default_exchange.publish.await_args
    assert kwargs["routing_key"] == "order.main.retry.4000ms"
    assert published[0].headers[RETRY_HEADER] == 2
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert "RuntimeError: db is down" in record.getMessage()
    assert "retry 2/3" in record.getMessage()


async def test_zero_delay_retries_straight_to_main_queue() -> None:
    bus = RabbitEventBus("amqp://x", "order", retry_delays=(0.0,))
    on_message, channel = await _subscribe(bus)
    await on_message(_message(retries=0))
    assert channel.default_exchange.publish.await_args.kwargs["routing_key"] == "order.main"


async def test_last_failure_goes_to_dead_letter_queue(caplog: pytest.LogCaptureFixture) -> None:
    bus = RabbitEventBus("amqp://x", "order", max_retries=3)
    on_message, channel = await _subscribe(bus)
    message = _message(retries=3)

    with caplog.at_level(logging.WARNING, logger="farm_common.events"):
        await on_message(message)

    message.reject.assert_awaited_once_with(requeue=False)
    message.ack.assert_not_awaited()
    channel.default_exchange.publish.assert_not_awaited()
    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert "order.main.dlq" in record.getMessage()
