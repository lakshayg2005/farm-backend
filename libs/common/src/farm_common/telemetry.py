"""OpenTelemetry for every service: traces, metrics and the trace id in each log line.

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (tests and plain local runs export nothing). When it
is set, each process sends over OTLP/HTTP to that endpoint (an OpenTelemetry Collector):

- traces: one span per HTTP request (FastAPI), per SQL statement (asyncpg), per outgoing HTTP call
  (httpx), and per event published or consumed (RabbitMQ, see farm_common.events)
- metrics: request rate, errors and latency per route (http.server.request.duration), event
  counters and handler latency, and process CPU / memory / GC for each service instance

The correlation id of a request is its trace id (farm_common.log), and the outbox keeps the
correlation id with every event. So when the relay publishes an event later, from a background
task, the publish span and every consumer span still land in the trace of the request that caused
the event: one user action can be followed across all services in Grafana / Tempo.

Standard OTEL_* variables apply, e.g. OTEL_TRACES_SAMPLER_ARG=0.1 keeps 10% of traces (metrics are
never sampled), OTEL_RESOURCE_ATTRIBUTES adds labels, OTEL_SDK_DISABLED=true turns everything off.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Sequence
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    TraceFlags,
)
from opentelemetry.util.types import Attributes

logger = logging.getLogger(__name__)

_enabled = False
_sample_ratio = 1.0

# Background loops (the outbox relay, expiry sweeps) run SQL outside any request. Those statements
# would each start a trace of one span every few seconds; they are dropped (metrics still count
# them via Postgres). Requests, event handlers and jobs start their own traces as usual.
_ORPHAN_KINDS = (SpanKind.CLIENT,)


def telemetry_enabled() -> bool:
    return _enabled


def _ratio_from_env() -> float:
    try:
        ratio = float(os.getenv("OTEL_TRACES_SAMPLER_ARG", "1.0"))
    except ValueError:
        return 1.0
    return min(max(ratio, 0.0), 1.0)


def setup_telemetry(service_name: str, version: str = "0.0.0") -> bool:
    """Start exporting traces and metrics for this process. Safe to call more than once.

    Returns True when telemetry is on. Call it before the app or worker starts handling work;
    the uvicorn / arq process calls it via create_app() or the worker's startup hook.
    """
    global _enabled, _sample_ratio
    if _enabled:
        return True
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_SDK_DISABLED") == "true":
        return False

    # New (stable) HTTP semantic conventions: http.server.request.duration in seconds,
    # with http.route, http.request.method and http.response.status_code.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import (
        Decision,
        ParentBased,
        Sampler,
        SamplingResult,
        TraceIdRatioBased,
    )

    _sample_ratio = _ratio_from_env()

    class _Sampler(Sampler):
        """ParentBased(TraceIdRatio) that also drops SQL / HTTP client spans with no parent."""

        def __init__(self, ratio: float) -> None:
            self._inner = ParentBased(TraceIdRatioBased(ratio))

        def should_sample(
            self,
            parent_context: Context | None,
            trace_id: int,
            name: str,
            kind: SpanKind | None = None,
            attributes: Attributes = None,
            links: Sequence[Any] | None = None,
            trace_state: Any = None,
        ) -> SamplingResult:
            parent = trace.get_current_span(parent_context).get_span_context()
            if not parent.is_valid and kind in _ORPHAN_KINDS:
                return SamplingResult(Decision.DROP)
            return self._inner.should_sample(
                parent_context, trace_id, name, kind, attributes, links, trace_state
            )

        def get_description(self) -> str:
            return f"FarmSampler({self._inner.get_description()})"

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "farm",
            "service.version": version,
            "service.instance.id": f"{socket.gethostname()}-{os.getpid()}",
            "deployment.environment.name": os.getenv("ENVIRONMENT", "local"),
        }
    )

    tracer_provider = TracerProvider(resource=resource, sampler=_Sampler(_sample_ratio))
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "10000"))
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=interval_ms)
        ],
    )
    metrics.set_meter_provider(meter_provider)

    AsyncPGInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    SystemMetricsInstrumentor(
        config={
            "process.cpu.time": ["user", "system"],
            "process.cpu.utilization": None,
            "process.memory.usage": None,
            "process.open_file_descriptor.count": None,
            "process.thread.count": None,
            "cpython.gc.collections": None,
        }
    ).instrument()

    _enabled = True
    logger.info(
        "Telemetry on: exporting to %s, keeping %.0f%% of traces",
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        _sample_ratio * 100,
    )
    return True


def instrument_fastapi(app: Any) -> None:
    """Request spans and http.server.* metrics for this app (no-op while telemetry is off)."""
    if not _enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="/health$,/metrics$")


def current_trace_id() -> str | None:
    """Trace id of the active span as 32 hex characters, or None outside a recorded trace."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def current_span_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.span_id, "016x") if ctx.is_valid else None


def _is_trace_id(value: str | None) -> bool:
    if not value or len(value) != 32:
        return False
    try:
        return int(value, 16) != 0
    except ValueError:
        return False


def context_from_correlation_id(correlation_id: str | None, seed: int) -> Context | None:
    """A parent context that puts a span into the trace whose id is `correlation_id`.

    Used when an event is published after the request has finished (the outbox relay): there is
    no active span, but the correlation id is the request's trace id. `seed` (e.g. from the event
    id) gives the synthetic parent span id. Returns None when there is an active span already or
    the correlation id is not a trace id (a client sent its own X-Correlation-ID).
    """
    if trace.get_current_span().get_span_context().is_valid or not _is_trace_id(correlation_id):
        return None
    trace_id = int(correlation_id, 16)  # type: ignore[arg-type]
    # Same keep/drop decision as TraceIdRatioBased made for the request (it only looks at the
    # lower 64 bits of the trace id), so a sampled request keeps its events and vice versa.
    bound = round(_sample_ratio * (2**64 - 1))
    sampled = (trace_id & 0xFFFFFFFFFFFFFFFF) < bound if _sample_ratio < 1.0 else True
    span_context = SpanContext(
        trace_id=trace_id,
        span_id=(seed & 0xFFFFFFFFFFFFFFFF) or 1,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT),
    )
    return trace.set_span_in_context(NonRecordingSpan(span_context), otel_context.get_current())


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def get_meter(name: str) -> metrics.Meter:
    return metrics.get_meter(name)


def traced_job(name: str | None = None) -> Any:
    """Decorator for background job functions (arq): one span per run and the
    farm.jobs.duration histogram (by job and outcome). Keeps the function's name for arq."""
    import functools
    import time

    def decorate(fn: Any) -> Any:
        job = name or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer("farm_common.jobs")
            started = time.perf_counter()
            outcome = "ok"
            with tracer.start_as_current_span(f"job {job}", kind=SpanKind.CONSUMER) as span:
                span.set_attribute("farm.job.name", job)
                try:
                    return await fn(*args, **kwargs)
                except BaseException:
                    outcome = "error"
                    raise
                finally:
                    _job_duration().record(
                        time.perf_counter() - started, {"job": job, "outcome": outcome}
                    )

        return wrapper

    return decorate


_job_histogram: metrics.Histogram | None = None


def _job_duration() -> metrics.Histogram:
    global _job_histogram
    if _job_histogram is None:
        _job_histogram = get_meter("farm_common.jobs").create_histogram(
            "farm.jobs.duration",
            unit="s",
            description="Run time of background jobs (pooling, media processing)",
            explicit_bucket_boundaries_advisory=[0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120],
        )
    return _job_histogram
