"""Telemetry glue: correlation id = trace id, trace ids in logs, events joining the request trace."""

import json
import logging
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from farm_common.events import Event, _to_message
from farm_common.log import CorrelationIdMiddleware, JsonFormatter
from farm_common.telemetry import context_from_correlation_id, setup_telemetry

_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
_tracer = _provider.get_tracer("test")


@pytest.fixture(autouse=True)
def _clear() -> None:
    _exporter.clear()


def test_setup_is_off_without_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert setup_telemetry("test") is False


def test_correlation_id_is_the_trace_id_of_the_request() -> None:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"trace": format(trace.get_current_span().get_span_context().trace_id, "032x")}

    FastAPIInstrumentor.instrument_app(app, tracer_provider=_provider)
    client = TestClient(app)

    response = client.get("/ping")
    assert response.headers["X-Correlation-ID"] == response.json()["trace"]

    # A correlation id sent by the client is kept as it is
    response = client.get("/ping", headers={"X-Correlation-ID": "from-client"})
    assert response.headers["X-Correlation-ID"] == "from-client"


def test_log_lines_carry_the_trace_and_span_id() -> None:
    formatter = JsonFormatter("test")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    assert "traceId" not in json.loads(formatter.format(record))
    with _tracer.start_as_current_span("work") as span:
        line = json.loads(formatter.format(record))
    assert line["traceId"] == format(span.get_span_context().trace_id, "032x")
    assert line["spanId"] == format(span.get_span_context().span_id, "016x")


def test_an_outbox_event_joins_the_trace_of_its_correlation_id() -> None:
    trace_id = uuid4().hex
    ctx = context_from_correlation_id(trace_id, seed=42)
    assert ctx is not None
    with _tracer.start_as_current_span("publish", context=ctx) as span:
        assert format(span.get_span_context().trace_id, "032x") == trace_id

    # Not a trace id (client-chosen correlation id) or no correlation id: a new trace
    assert context_from_correlation_id("from-client", seed=1) is None
    assert context_from_correlation_id(None, seed=1) is None
    assert context_from_correlation_id("0" * 32, seed=1) is None


def test_the_active_span_wins_over_the_correlation_id() -> None:
    with _tracer.start_as_current_span("request"):
        assert context_from_correlation_id(uuid4().hex, seed=1) is None


def test_messages_carry_the_traceparent_of_the_active_span() -> None:
    event = Event(type="order.placed", producer="order", correlation_id="abc")
    assert "traceparent" not in _to_message(event).headers

    with _tracer.start_as_current_span("publish") as span:
        headers = _to_message(event).headers
    trace_hex = format(span.get_span_context().trace_id, "032x")
    assert str(headers["traceparent"]).split("-")[1] == trace_hex
    assert headers["x-correlation-id"] == "abc"
