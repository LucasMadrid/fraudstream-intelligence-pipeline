"""Prometheus metrics for the API channel producer."""

import logging
import os

from prometheus_client import Counter, Gauge, Histogram, start_http_server

logger = logging.getLogger(__name__)

PUBLISH_LATENCY = Histogram(
    "producer_publish_latency_ms",
    "End-to-end publish latency in milliseconds",
    labelnames=["topic"],
    buckets=[1, 2, 5, 10, 20, 50, 100, 500],
)

EVENTS_TOTAL = Counter(
    "producer_events_total",
    "Total events attempted (accepted or rejected)",
    labelnames=["topic", "status"],
)

ERRORS_TOTAL = Counter(
    "producer_errors_total",
    "Total producer errors by type",
    labelnames=["topic", "error_type"],
)

DLQ_DEPTH = Gauge(
    "dlq_depth",
    "Estimated number of messages sent to the DLQ (monotonically increasing)",
    labelnames=["topic"],
)

SCHEMA_VALIDATION_ERRORS = Counter(
    "schema_validation_errors_total",
    "Total schema and field-level validation errors",
    labelnames=["topic", "error_type"],
)

_metrics_server_started = False


def start_metrics_server(port: int | None = None) -> None:
    """Start the Prometheus HTTP scrape endpoint (idempotent)."""
    global _metrics_server_started
    if _metrics_server_started:
        return
    effective_port = port or int(os.environ.get("PROMETHEUS_PORT", "8001"))
    start_http_server(effective_port)
    _metrics_server_started = True
    logger.info("prometheus_metrics_server_started", extra={"port": effective_port})
