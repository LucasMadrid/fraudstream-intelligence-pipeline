"""API channel Kafka producer: validate → mask → serialize → publish."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from confluent_kafka import SerializingProducer
from confluent_kafka.schema_registry.avro import AvroSerializer

from pipelines.ingestion.api.config import ProducerConfig
from pipelines.ingestion.api.metrics import (
    DLQ_DEPTH,
    ERRORS_TOTAL,
    EVENTS_TOTAL,
    PUBLISH_LATENCY,
    SCHEMA_VALIDATION_ERRORS,
    start_metrics_server,
)
from pipelines.ingestion.api.telemetry import get_tracer, init_tracer
from pipelines.ingestion.shared.dlq_producer import DLQProducer
from pipelines.ingestion.shared.pii_masker import (
    InvalidPANError,
    MaskingConfig,
    RawPAN,
    truncate_ip,
)
from pipelines.ingestion.shared.schema_registry import connect_with_retry, load_schema_str

logger = logging.getLogger(__name__)

TOPIC = "txn.api"
MASKING_LIB_VERSION = "1.0.0"
VALID_CHANNELS = {"POS", "WEB", "MOBILE", "API"}

_SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"


class MaskingError(RuntimeError):
    """Raised when PII masking fails or a PII leak is detected."""


class ValidationError(ValueError):
    """Raised when a payload fails field-level validation."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_required_fields(payload: dict) -> list[str]:
    """Return list of missing required field names. Raises ValidationError if non-empty."""
    required = [
        "transaction_id",
        "account_id",
        "merchant_id",
        "amount",
        "currency",
        "event_time",
        "channel",
        "card_number",
        "caller_ip",
        "api_key_id",
        "oauth_scope",
    ]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        raise ValidationError(f"Missing required fields: {missing}")
    return missing


def validate_field_values(payload: dict) -> list[str]:
    """Validate field semantics.

    Returns list of error strings. Raises ValidationError if non-empty.
    """
    errors: list[str] = []

    try:
        amount = float(payload.get("amount", 0))
        if amount <= 0:
            errors.append("amount must be > 0")
    except (TypeError, ValueError):
        errors.append("amount must be a numeric value")

    currency = payload.get("currency", "")
    if not isinstance(currency, str) or len(currency) != 3:
        errors.append("currency must be a 3-character ISO-4217 code")

    channel = payload.get("channel", "")
    if channel not in VALID_CHANNELS:
        errors.append(f"channel must be one of {sorted(VALID_CHANNELS)}")

    if not payload.get("api_key_id"):
        errors.append("api_key_id must not be empty")

    if not payload.get("oauth_scope"):
        errors.append("oauth_scope must not be empty")

    event_time = payload.get("event_time")
    if event_time is not None:
        now_ms = int(time.time() * 1000)
        skew_ms = abs(now_ms - int(event_time))
        if skew_ms > 5 * 60 * 1000:
            errors.append("event_time is more than 5 minutes from server time")

    if errors:
        raise ValidationError(f"Field validation errors: {errors}")
    return errors


# ---------------------------------------------------------------------------
# Event builder
# ---------------------------------------------------------------------------


class TransactionEventBuilder:
    """Builds a validated, masked event dict matching txn-api-v1.avsc."""

    def __init__(self, cfg: MaskingConfig) -> None:
        self._cfg = cfg

    def build(self, raw_payload: dict) -> dict:
        """Apply masking and build the event dict. Raises MaskingError on failure."""
        try:
            raw_pan = RawPAN(raw_payload["card_number"])
            masked = raw_pan.mask(self._cfg)
        except InvalidPANError:
            raise
        except Exception as exc:
            raise MaskingError(f"PAN masking failed: {exc}") from exc

        try:
            subnet = truncate_ip(raw_payload["caller_ip"], self._cfg)
        except Exception as exc:
            raise MaskingError(f"IP masking failed: {exc}") from exc

        processing_time = int(time.time() * 1000)

        # Encode amount as bytes (big-endian decimal scaled by 10^4)
        raw_amount = raw_payload["amount"]
        amount_int = round(float(raw_amount) * 10_000)
        # Avro decimal bytes: big-endian two's complement
        byte_length = (amount_int.bit_length() + 8) // 8  # +1 sign bit, round up
        amount_bytes = amount_int.to_bytes(byte_length, byteorder="big", signed=True)

        event: dict[str, Any] = {
            "transaction_id": raw_payload.get("transaction_id") or str(uuid.uuid4()),
            "account_id": raw_payload["account_id"],
            "merchant_id": raw_payload["merchant_id"],
            "amount": amount_bytes,
            "currency": raw_payload["currency"],
            "event_time": int(raw_payload["event_time"]),
            "processing_time": processing_time,
            "channel": "API",
            "card_bin": masked.bin6,
            "card_last4": masked.last4,
            "caller_ip_subnet": subnet,
            "api_key_id": raw_payload["api_key_id"],
            "oauth_scope": raw_payload["oauth_scope"],
            "geo_lat": raw_payload.get("geo_lat"),
            "geo_lon": raw_payload.get("geo_lon"),
            "masking_lib_version": MASKING_LIB_VERSION,
            "schema_version": "1",
        }

        # Enforce no PAN leakage
        assert "card_number" not in event, "BUG: card_number must not appear in event dict"
        return event


# ---------------------------------------------------------------------------
# Publish result
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    transaction_id: str
    status: str
    latency_ms: int


# ---------------------------------------------------------------------------
# Producer service
# ---------------------------------------------------------------------------


class ProducerService:
    """Manages the Kafka SerializingProducer lifecycle for the API channel."""

    def __init__(self, config: ProducerConfig | None = None) -> None:
        self._config = config or ProducerConfig()
        self._masking_cfg = MaskingConfig(
            ipv4_prefix=self._config.ipv4_prefix,
            ipv6_prefix=self._config.ipv6_prefix,
        )
        self._builder = TransactionEventBuilder(self._masking_cfg)
        self._producer: SerializingProducer | None = None
        self._dlq_producer: DLQProducer | None = None
        self._schema_str: str | None = None
        self._serializer: AvroSerializer | None = None

    def start(self) -> None:
        """Connect to Schema Registry, wire serializer, and start the Kafka producer."""
        schema_path = _SCHEMAS_DIR / "txn_api_v1.avsc"
        self._schema_str = load_schema_str(schema_path)

        sr_wrapper = connect_with_retry(
            self._config.schema_registry_url,
            retries=self._config.schema_registry_retries,
        )
        self._serializer = sr_wrapper.get_serializer("txn.api-value", self._schema_str)

        librdkafka_cfg = self._config.librdkafka_config()
        librdkafka_cfg["value.serializer"] = self._serializer
        self._producer = SerializingProducer(librdkafka_cfg)

        self._dlq_producer = DLQProducer(
            bootstrap_servers=self._config.bootstrap_servers
        )

        start_metrics_server(self._config.prometheus_port)
        init_tracer()

        logger.info(
            "api_producer_started",
            extra={
                "component": "api-producer",
                "schema_registry": self._config.schema_registry_url,
                "topic": TOPIC,
            },
        )

    def publish(self, raw_payload: dict) -> PublishResult:
        """Validate, mask, serialize, and produce one transaction event."""
        assert self._producer is not None, "ProducerService.start() must be called first"

        tracer = get_tracer()
        t0 = time.monotonic()

        with tracer.start_as_current_span("api.producer.publish") as span:
            transaction_id = raw_payload.get("transaction_id") or str(uuid.uuid4())
            span.set_attribute("transaction_id", transaction_id)
            span.set_attribute("channel", "API")
            span.set_attribute("topic", TOPIC)
            span.set_attribute("schema_version", "1")

            masking_applied = False
            try:
                # ---- PII guard: masking happens here ----
                event = self._builder.build(raw_payload)
                masking_applied = True

                # Explicit PII leak guard before produce()
                _assert_no_pii_leak(event)

                # Produce
                self._producer.produce(
                    topic=TOPIC,
                    key=event["account_id"],
                    value=event,
                    on_delivery=self._delivery_callback,
                )
                self._producer.poll(0)

                latency_ms = int((time.monotonic() - t0) * 1000)
                PUBLISH_LATENCY.labels(topic=TOPIC).observe(latency_ms)
                EVENTS_TOTAL.labels(topic=TOPIC, status="accepted").inc()

                logger.info(
                    "published",
                    extra={
                        "transaction_id": transaction_id,
                        "component": "api-producer",
                        "timestamp": int(time.time() * 1000),
                        "log_level": "INFO",
                        "event": "published",
                        "latency_ms": latency_ms,
                    },
                )
                return PublishResult(
                    transaction_id=transaction_id,
                    status="accepted",
                    latency_ms=latency_ms,
                )

            except (InvalidPANError, MaskingError) as exc:
                _handle_pre_serialise_error(
                    exc,
                    raw_payload,
                    masking_applied,
                    self._dlq_producer,
                    span,
                )
                raise

    def _delivery_callback(self, err: Any, msg: Any) -> None:
        """Async Kafka delivery callback — routes broker errors to DLQ."""
        if err is not None:
            logger.error(
                "delivery_failed",
                extra={
                    "component": "api-producer",
                    "topic": msg.topic() if msg else TOPIC,
                    "error": str(err),
                },
            )
            ERRORS_TOTAL.labels(topic=TOPIC, error_type="BrokerError").inc()
            DLQ_DEPTH.labels(topic="txn.api.dlq").inc()
            if self._dlq_producer:
                # Fire-and-forget to DLQ — never flush() inside callback
                try:
                    self._dlq_producer.send_to_dlq(
                        source_topic=TOPIC,
                        original_payload="{}",  # masked payload not available here
                        error_type="BrokerError",
                        error_message=str(err),
                        masking_applied=True,
                    )
                except Exception:
                    logger.exception("dlq_write_failed_in_callback")

    def flush(self, timeout: float = 5.0) -> None:
        if self._producer:
            self._producer.flush(timeout)


# ---------------------------------------------------------------------------
# PII leak guard (T026)
# ---------------------------------------------------------------------------

_PII_PATTERN = re.compile(r"\b\d{15,19}\b")


def _assert_no_pii_leak(event: dict) -> None:
    """Raise MaskingError if any event value looks like a full card number."""
    if "card_number" in event:
        raise MaskingError("PII LEAK: 'card_number' key present in event dict")
    for key, value in event.items():
        if isinstance(value, str) and _PII_PATTERN.fullmatch(value):
            raise MaskingError(
                f"PII LEAK: field {key!r} contains a full card-number-length digit sequence"
            )


def _handle_pre_serialise_error(
    exc: Exception,
    raw_payload: dict,
    masking_applied: bool,
    dlq_producer: DLQProducer | None,
    span: Any,
) -> None:
    error_type = type(exc).__name__
    ERRORS_TOTAL.labels(topic=TOPIC, error_type=error_type).inc()
    DLQ_DEPTH.labels(topic="txn.api.dlq").inc()
    span.add_event("dlq.routed", attributes={"error_type": error_type})

    # Sanitise payload before writing to DLQ if masking was not applied
    safe_payload = json.dumps({
        k: v for k, v in raw_payload.items() if k not in ("card_number", "caller_ip")
    })

    if dlq_producer:
        try:
            dlq_producer.send_to_dlq(
                source_topic=TOPIC,
                original_payload=safe_payload,
                error_type=error_type,
                error_message=str(exc),
                masking_applied=masking_applied,
            )
        except Exception:
            logger.exception("dlq_write_failed")

    logger.error(
        "publish_failed",
        extra={
            "component": "api-producer",
            "error_type": error_type,
            "masking_applied": masking_applied,
            "detail": str(exc),
        },
    )


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _RequestHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for POST /v1/transactions."""

    producer_service: ProducerService  # set at startup

    def log_message(self, fmt: str, *args: Any) -> None:  # suppress default access log
        pass

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/transactions":
            self._respond(404, {"error": "NotFound"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "InvalidJSON"})
            return

        try:
            validate_required_fields(payload)
            validate_field_values(payload)
        except ValidationError as exc:
            fields = [str(e) for e in exc.args]
            ERRORS_TOTAL.labels(topic=TOPIC, error_type="ValidationError").inc()
            SCHEMA_VALIDATION_ERRORS.labels(topic=TOPIC, error_type="ValidationError").inc()
            logger.warning(
                "validation_failed",
                extra={
                    "component": "api-producer",
                    "level": "WARNING",
                    "event": "validation_failed",
                    "fields": fields,
                },
            )
            self._respond(400, {"error": "ValidationError", "fields": fields})
            return

        try:
            result = self.producer_service.publish(payload)
            self._respond(200, {
                "transaction_id": result.transaction_id,
                "status": result.status,
                "latency_ms": result.latency_ms,
            })
        except InvalidPANError as exc:
            self._respond(400, {"error": "InvalidPANError", "detail": str(exc)})
        except MaskingError as exc:
            self._respond(500, {"error": "MaskingError", "detail": str(exc)})
        except Exception as exc:
            logger.exception("unexpected_publish_error")
            self._respond(500, {"error": "InternalError", "detail": str(exc)})

    def _respond(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start the HTTP server (blocking)."""
    import logging as _logging
    import os

    _logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format=(
            '{"timestamp": "%(asctime)s", "level": "%(levelname)s",'
            ' "component": "api-producer", "event": "%(message)s"}'
        ),
    )

    config = ProducerConfig()
    service = ProducerService(config)
    service.start()

    _RequestHandler.producer_service = service

    server = HTTPServer((host, port), _RequestHandler)
    logger.info(
        "server_ready",
        extra={"component": "api-producer", "host": host, "port": port},
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        service.flush()
        server.server_close()


if __name__ == "__main__":
    run_server()
