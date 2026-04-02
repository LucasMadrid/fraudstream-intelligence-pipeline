"""OTel W3C trace context propagation through the enrichment pipeline."""

from __future__ import annotations

from opentelemetry import propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Use a no-op exporter by default; real exporter injected via OTEL_EXPORTER_* env vars
_provider = TracerProvider()
_noop_exporter = InMemorySpanExporter()
_provider.add_span_processor(SimpleSpanProcessor(_noop_exporter))

tracer = _provider.get_tracer("fraudstream.processing.enrichment")

# W3C Trace Context header name as used in Kafka record headers
TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"


def extract_trace_context(headers: list[tuple[str, bytes]] | None) -> dict:
    """Extract W3C traceparent/tracestate from Kafka record headers.

    Returns a carrier dict suitable for ``propagate.extract``.
    Returns an empty dict if headers are absent (best-effort — no-op).
    """
    if not headers:
        return {}
    carrier: dict[str, str] = {}
    for key, value in headers:
        if key in (TRACEPARENT_HEADER, TRACESTATE_HEADER):
            carrier[key] = value.decode("utf-8", errors="replace")
    return carrier


def inject_trace_context(carrier: dict) -> list[tuple[str, bytes]]:
    """Inject the active W3C trace context into a dict and return as Kafka headers.

    Returns an empty list if no active trace (best-effort).
    """
    propagate.inject(carrier)
    headers = []
    for key in (TRACEPARENT_HEADER, TRACESTATE_HEADER):
        if key in carrier:
            headers.append((key, carrier[key].encode("utf-8")))
    return headers
