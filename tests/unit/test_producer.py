"""Unit tests for the API channel producer (no Kafka required)."""

import time

import pytest

from pipelines.ingestion.api.producer import (
    MaskingError,
    TransactionEventBuilder,
    ValidationError,
    _assert_no_pii_leak,
    validate_field_values,
    validate_required_fields,
)
from pipelines.ingestion.shared.pii_masker import MaskingConfig

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
# validate_required_fields
# ---------------------------------------------------------------------------


def test_validate_required_all_present():
    validate_required_fields(VALID_PAYLOAD)  # no exception


def test_validate_required_missing_transaction_id():
    payload = {**VALID_PAYLOAD, "transaction_id": ""}
    with pytest.raises(ValidationError):
        validate_required_fields(payload)


def test_validate_required_missing_account_id():
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "account_id"}
    with pytest.raises(ValidationError):
        validate_required_fields(payload)


# ---------------------------------------------------------------------------
# validate_field_values
# ---------------------------------------------------------------------------


def test_validate_fields_valid():
    validate_field_values(VALID_PAYLOAD)  # no exception


def test_validate_fields_negative_amount():
    payload = {**VALID_PAYLOAD, "amount": "-1"}
    with pytest.raises(ValidationError, match="amount"):
        validate_field_values(payload)


def test_validate_fields_invalid_currency():
    payload = {**VALID_PAYLOAD, "currency": "USDX"}
    with pytest.raises(ValidationError, match="currency"):
        validate_field_values(payload)


def test_validate_fields_invalid_channel():
    payload = {**VALID_PAYLOAD, "channel": "MOBILE_APP"}
    with pytest.raises(ValidationError, match="channel"):
        validate_field_values(payload)


# ---------------------------------------------------------------------------
# TransactionEventBuilder
# ---------------------------------------------------------------------------


def test_builder_produces_masked_event():
    cfg = MaskingConfig()
    builder = TransactionEventBuilder(cfg)
    event = builder.build(VALID_PAYLOAD)
    assert "card_number" not in event
    assert event["card_bin"] == "411111"
    assert event["card_last4"] == "1111"
    assert event["caller_ip_subnet"] == "203.0.113.0"
    assert event["channel"] == "API"


def test_builder_sets_processing_time():
    cfg = MaskingConfig()
    builder = TransactionEventBuilder(cfg)
    before = int(time.time() * 1000)
    event = builder.build(VALID_PAYLOAD)
    after = int(time.time() * 1000)
    assert before <= event["processing_time"] <= after


def test_builder_rejects_invalid_pan():
    from pipelines.ingestion.shared.pii_masker import InvalidPANError

    cfg = MaskingConfig()
    builder = TransactionEventBuilder(cfg)
    bad = {**VALID_PAYLOAD, "card_number": "1234567890123456"}
    with pytest.raises(InvalidPANError):
        builder.build(bad)


# ---------------------------------------------------------------------------
# PII leak guard
# ---------------------------------------------------------------------------


def test_pii_guard_blocks_card_number_key():
    with pytest.raises(MaskingError, match="card_number"):
        _assert_no_pii_leak({"card_number": "4111111111111111", "other": "x"})


def test_pii_guard_blocks_digit_sequence():
    with pytest.raises(MaskingError, match="PII LEAK"):
        _assert_no_pii_leak({"some_field": "4111111111111111"})


def test_pii_guard_passes_clean_event():
    event = {
        "card_bin": "411111",
        "card_last4": "1111",
        "caller_ip_subnet": "203.0.113.0",
        "account_id": "acc_001",
    }
    _assert_no_pii_leak(event)  # no exception
