"""Extended unit tests to reach 80% coverage on producer.py, metrics, telemetry, dlq."""

import json
import time
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from pipelines.ingestion.api.producer import (
    MaskingError,
    TransactionEventBuilder,
    ValidationError,
    _handle_pre_serialise_error,
    _RequestHandler,
    validate_field_values,
)
from pipelines.ingestion.shared.pii_masker import InvalidPANError, MaskingConfig

VALID_PAYLOAD = {
    "transaction_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "account_id": "acc_001",
    "merchant_id": "mch_xyz",
    "amount": "49.99",
    "currency": "USD",
    "card_number": "4111111111111111",
    "caller_ip": "203.0.113.45",
    "api_key_id": "key_abc123",
    "oauth_scope": "transactions:write",
    "event_time": int(time.time() * 1000),
    "channel": "API",
}


# ---------------------------------------------------------------------------
# validate_field_values edge cases
# ---------------------------------------------------------------------------


def test_validate_fields_non_numeric_amount():
    payload = {**VALID_PAYLOAD, "amount": "not-a-number"}
    with pytest.raises(ValidationError, match="numeric"):
        validate_field_values(payload)


def test_validate_fields_empty_api_key():
    payload = {**VALID_PAYLOAD, "api_key_id": ""}
    with pytest.raises(ValidationError, match="api_key_id"):
        validate_field_values(payload)


def test_validate_fields_empty_oauth_scope():
    payload = {**VALID_PAYLOAD, "oauth_scope": ""}
    with pytest.raises(ValidationError, match="oauth_scope"):
        validate_field_values(payload)


def test_validate_fields_stale_event_time():
    stale = int((time.time() - 400) * 1000)  # 6+ minutes ago
    payload = {**VALID_PAYLOAD, "event_time": stale}
    with pytest.raises(ValidationError, match="event_time"):
        validate_field_values(payload)


def test_validate_fields_zero_amount():
    payload = {**VALID_PAYLOAD, "amount": "0"}
    with pytest.raises(ValidationError, match="amount"):
        validate_field_values(payload)


# ---------------------------------------------------------------------------
# TransactionEventBuilder — edge cases
# ---------------------------------------------------------------------------


def test_builder_masking_error_on_bad_ip():
    cfg = MaskingConfig()
    builder = TransactionEventBuilder(cfg)
    bad = {**VALID_PAYLOAD, "caller_ip": "not-an-ip"}
    with pytest.raises(MaskingError, match="IP masking"):
        builder.build(bad)


def test_builder_optional_geo_fields():
    cfg = MaskingConfig()
    builder = TransactionEventBuilder(cfg)
    payload = {**VALID_PAYLOAD, "geo_lat": 51.5, "geo_lon": -0.1}
    event = builder.build(payload)
    assert event["geo_lat"] == 51.5
    assert event["geo_lon"] == -0.1


def test_builder_auto_generates_transaction_id():
    cfg = MaskingConfig()
    builder = TransactionEventBuilder(cfg)
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "transaction_id"}
    event = builder.build(payload)
    assert event["transaction_id"]  # UUID auto-generated


def test_builder_amount_encoded_as_bytes():
    cfg = MaskingConfig()
    builder = TransactionEventBuilder(cfg)
    event = builder.build(VALID_PAYLOAD)
    assert isinstance(event["amount"], bytes)


# ---------------------------------------------------------------------------
# _handle_pre_serialise_error
# ---------------------------------------------------------------------------


def test_handle_error_calls_dlq_producer():
    mock_dlq = MagicMock()
    mock_span = MagicMock()
    exc = InvalidPANError("Luhn check failed")

    _handle_pre_serialise_error(exc, VALID_PAYLOAD, False, mock_dlq, mock_span)

    mock_dlq.send_to_dlq.assert_called_once()
    call_kwargs = mock_dlq.send_to_dlq.call_args[1]
    assert call_kwargs["error_type"] == "InvalidPANError"
    assert call_kwargs["masking_applied"] is False


def test_handle_error_with_no_dlq_producer():
    """Should not raise when dlq_producer is None."""
    mock_span = MagicMock()
    exc = MaskingError("masking failed")
    _handle_pre_serialise_error(exc, VALID_PAYLOAD, True, None, mock_span)  # no exception


def test_handle_error_dlq_write_failure_does_not_propagate():
    """DLQ write failure must not propagate — swallowed with a log."""
    mock_dlq = MagicMock()
    mock_dlq.send_to_dlq.side_effect = RuntimeError("DLQ broker down")
    mock_span = MagicMock()
    exc = InvalidPANError("bad pan")
    # Must not raise
    _handle_pre_serialise_error(exc, VALID_PAYLOAD, False, mock_dlq, mock_span)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_start_metrics_server_idempotent():
    """Calling start_metrics_server twice should not raise."""
    from pipelines.ingestion.api import metrics as m

    m._metrics_server_started = False  # reset for test

    with patch("pipelines.ingestion.api.metrics.start_http_server") as mock_start:
        from pipelines.ingestion.api.metrics import start_metrics_server

        start_metrics_server(9999)
        start_metrics_server(9999)  # second call is a no-op
        mock_start.assert_called_once()

    m._metrics_server_started = False  # cleanup


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_get_tracer_returns_tracer():
    from pipelines.ingestion.api import telemetry as t

    t._tracer = None
    tracer = t.get_tracer()
    assert tracer is not None
    t._tracer = None  # cleanup


def test_init_tracer_idempotent():
    from pipelines.ingestion.api import telemetry as t

    t._tracer = None
    tracer1 = t.init_tracer()
    tracer2 = t.init_tracer()  # should return cached
    assert tracer1 is tracer2
    t._tracer = None


def test_init_tracer_with_endpoint():
    """Test OTLP path — patch the submodule import that happens inside init_tracer."""
    import sys

    from pipelines.ingestion.api import telemetry as t

    t._tracer = None

    # The tracer module does: from opentelemetry.exporter.otlp... import OTLPSpanExporter
    # inside the function body. We inject a fake module so the import succeeds.
    fake_exporter_cls = MagicMock()
    fake_exporter_cls.return_value = MagicMock()
    fake_module = MagicMock()
    fake_module.OTLPSpanExporter = fake_exporter_cls

    with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"}):
        with patch.dict(
            sys.modules,
            {"opentelemetry.exporter.otlp.proto.grpc.trace_exporter": fake_module},
        ):
            tracer = t.init_tracer()
            assert tracer is not None

    t._tracer = None


# ---------------------------------------------------------------------------
# DLQProducer
# ---------------------------------------------------------------------------


def test_dlq_producer_send_calls_produce():
    with patch("pipelines.ingestion.shared.dlq_producer.Producer") as MockProducer:
        instance = MockProducer.return_value
        from pipelines.ingestion.shared.dlq_producer import DLQProducer

        dlq = DLQProducer(bootstrap_servers="localhost:9092")
        dlq.send_to_dlq(
            source_topic="txn.api",
            original_payload='{"account_id": "x"}',
            error_type="TestError",
            error_message="test message",
            masking_applied=True,
        )

        instance.produce.assert_called_once()
        call_kwargs = instance.produce.call_args[1]
        assert call_kwargs["topic"] == "txn.api.dlq"
        headers = dict(call_kwargs["headers"])
        assert headers["error_type"] == "TestError"

        value = json.loads(call_kwargs["value"])
        assert value["error_type"] == "TestError"
        assert value["masking_applied"] is True
        assert value["source_topic"] == "txn.api"


def test_dlq_producer_flush():
    with patch("pipelines.ingestion.shared.dlq_producer.Producer") as MockProducer:
        instance = MockProducer.return_value
        from pipelines.ingestion.shared.dlq_producer import DLQProducer

        dlq = DLQProducer(bootstrap_servers="localhost:9092")
        dlq.flush(timeout=1.0)
        instance.flush.assert_called_once_with(1.0)


# ---------------------------------------------------------------------------
# HTTP handler — minimal request/response testing
# ---------------------------------------------------------------------------


class _FakeSocket:
    """Minimal socket-like object for BaseHTTPRequestHandler tests."""

    def __init__(self, data: bytes):
        self._in = BytesIO(data)
        self._out = BytesIO()

    def makefile(self, mode: str, *args, **kwargs):
        if mode.startswith("r"):
            return self._in
        return self._out

    def sendall(self, data: bytes):
        self._out.write(data)


def _make_handler(method: str, path: str, body: dict | None, producer_service=None):
    """Construct a _RequestHandler for the given request."""
    body_bytes = json.dumps(body).encode() if body else b""
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"\r\n"
    ).encode() + body_bytes

    sock = _FakeSocket(raw)
    handler = _RequestHandler.__new__(_RequestHandler)
    handler.connection = sock
    handler.rfile = sock._in
    handler.wfile = sock._out
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = method
    handler.path = path
    handler.headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body_bytes)),
    }

    if producer_service:
        _RequestHandler.producer_service = producer_service
    return handler, sock


def test_http_handler_404_on_wrong_path():
    mock_service = MagicMock()
    handler, sock = _make_handler("POST", "/wrong/path", {}, mock_service)

    responses = []

    def _fake_respond(status: int, body: dict) -> None:
        responses.append((status, body))

    handler._respond = _fake_respond
    handler.do_POST()

    assert responses[0][0] == 404


def test_http_handler_400_on_invalid_json():
    mock_service = MagicMock()

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.path = "/v1/transactions"
    handler.command = "POST"
    handler.headers = {"Content-Length": "5"}
    handler.rfile = BytesIO(b"{bad}")
    handler.producer_service = mock_service

    responses = []
    handler._respond = lambda s, b: responses.append((s, b))
    handler.do_POST()

    assert responses[0][0] == 400
    assert responses[0][1]["error"] == "InvalidJSON"


def test_http_handler_400_on_validation_error():
    mock_service = MagicMock()

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.path = "/v1/transactions"
    payload = {**VALID_PAYLOAD, "transaction_id": ""}
    body = json.dumps(payload).encode()
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler.producer_service = mock_service

    responses = []
    handler._respond = lambda s, b: responses.append((s, b))
    handler.do_POST()

    assert responses[0][0] == 400
    assert responses[0][1]["error"] == "ValidationError"


def test_http_handler_200_on_valid_payload():
    mock_service = MagicMock()
    from pipelines.ingestion.api.producer import PublishResult

    mock_service.publish.return_value = PublishResult(
        transaction_id="test-txn-id", status="accepted", latency_ms=5
    )

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.path = "/v1/transactions"
    body = json.dumps(VALID_PAYLOAD).encode()
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    _RequestHandler.producer_service = mock_service

    responses = []
    handler._respond = lambda s, b: responses.append((s, b))
    handler.do_POST()

    assert responses[0][0] == 200
    assert responses[0][1]["transaction_id"] == "test-txn-id"


def test_http_handler_400_on_invalid_pan_error():
    mock_service = MagicMock()
    mock_service.publish.side_effect = InvalidPANError("Luhn check failed")

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.path = "/v1/transactions"
    body = json.dumps(VALID_PAYLOAD).encode()
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    _RequestHandler.producer_service = mock_service

    responses = []
    handler._respond = lambda s, b: responses.append((s, b))
    handler.do_POST()

    assert responses[0][0] == 400
    assert responses[0][1]["error"] == "InvalidPANError"


def test_http_handler_500_on_masking_error():
    mock_service = MagicMock()
    mock_service.publish.side_effect = MaskingError("PII leak detected")

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.path = "/v1/transactions"
    body = json.dumps(VALID_PAYLOAD).encode()
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    _RequestHandler.producer_service = mock_service

    responses = []
    handler._respond = lambda s, b: responses.append((s, b))
    handler.do_POST()

    assert responses[0][0] == 500
    assert responses[0][1]["error"] == "MaskingError"


def test_http_handler_500_on_unexpected_error():
    mock_service = MagicMock()
    mock_service.publish.side_effect = RuntimeError("unexpected")

    handler = _RequestHandler.__new__(_RequestHandler)
    handler.path = "/v1/transactions"
    body = json.dumps(VALID_PAYLOAD).encode()
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    _RequestHandler.producer_service = mock_service

    responses = []
    handler._respond = lambda s, b: responses.append((s, b))
    handler.do_POST()

    assert responses[0][0] == 500


def test_http_respond_writes_json():
    """_respond should write HTTP response with correct status and JSON body."""
    handler = _RequestHandler.__new__(_RequestHandler)
    out = BytesIO()
    handler.wfile = out
    handler.requestline = "POST /v1/transactions HTTP/1.1"

    # Patch send_response and send_header/end_headers to avoid full HTTP machinery
    calls = []
    handler.send_response = lambda s: calls.append(("status", s))
    handler.send_header = lambda k, v: calls.append(("header", k, v))
    handler.end_headers = lambda: calls.append(("end_headers",))

    handler._respond(200, {"ok": True})

    status_calls = [c for c in calls if c[0] == "status"]
    assert status_calls[0][1] == 200
    written = out.getvalue()
    assert b'"ok": true' in written
