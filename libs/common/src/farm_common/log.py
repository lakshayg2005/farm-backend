"""Correlation IDs and structured (JSON) logs.

Every request gets an X-Correlation-ID (taken from the incoming header, otherwise the request's
OpenTelemetry trace id, otherwise a random id). It is returned in the response, added to every log
line, copied into published events, and restored when an event is consumed, so one user action can
be traced across all services. Log lines also carry traceId / spanId while a span is active, which
Grafana uses to jump from a log line to its trace.
"""

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from farm_common.telemetry import current_span_id, current_trace_id

CORRELATION_HEADER = "X-Correlation-ID"

logger = logging.getLogger(__name__)

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


class CorrelationIdMiddleware:
    """Pure ASGI middleware, so the ID is also set inside exception handlers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = CORRELATION_HEADER.lower().encode()
        incoming = dict(scope["headers"]).get(header, b"").decode()[:64]
        correlation_id = incoming or current_trace_id() or new_correlation_id()
        token = _correlation_id.set(correlation_id)

        response_started = False

        async def send_with_header(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                message.setdefault("headers", [])
                message["headers"].append((header, correlation_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        except Exception:
            # Crashes are handled here rather than by Starlette's outer error middleware, which
            # runs after this middleware has exited and would lose the correlation ID.
            logger.exception("Unhandled error")
            if response_started:
                raise
            body = json.dumps(
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Something went wrong",
                        "correlationId": correlation_id,
                    }
                }
            ).encode()
            await send_with_header(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
        finally:
            _correlation_id.reset(token)


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
            "correlationId": get_correlation_id(),
        }
        trace_id = current_trace_id()
        if trace_id:
            entry["traceId"] = trace_id
            entry["spanId"] = current_span_id()
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(service_name: str, level: str = "INFO") -> None:
    """Send all logs (including uvicorn's) to stdout as one JSON object per line."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
