"""OpenTelemetry tracer setup for the API channel producer."""

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)

_tracer: Tracer | None = None


def init_tracer(service_name: str = "api-producer") -> Tracer:
    """Initialize the OTel tracer. Safe to call multiple times (idempotent)."""
    global _tracer
    if _tracer is not None:
        return _tracer

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    provider: TracerProvider

    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        logger.info("otel_tracer_initialized", extra={"endpoint": endpoint})
    else:
        # No-op tracer when endpoint not configured
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        logger.info("otel_tracer_noop", extra={"reason": "OTEL_EXPORTER_OTLP_ENDPOINT not set"})

    _tracer = trace.get_tracer(service_name)
    return _tracer


def get_tracer() -> Tracer:
    """Return the initialized tracer, initializing with no-op if not yet set up."""
    global _tracer
    if _tracer is None:
        return init_tracer()
    return _tracer
