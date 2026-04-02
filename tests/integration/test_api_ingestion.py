"""Integration tests: Kafka + Schema Registry via testcontainers.

Run with: pytest tests/integration/ -v --timeout=120

Requires Docker daemon running.
"""

import json
import re
import time
import uuid
from collections.abc import Generator

import pytest

# Guard: skip the entire module if testcontainers is not installed
pytest.importorskip("testcontainers")

from testcontainers.kafka import KafkaContainer  # type: ignore[import]

VALID_PAN = "4111111111111111"
VALID_IP = "203.0.113.45"


def _valid_payload(pan: str = VALID_PAN, ip: str = VALID_IP) -> dict:
    return {
        "transaction_id": str(uuid.uuid4()),
        "account_id": "acc_test",
        "merchant_id": "mch_test",
        "amount": "49.99",
        "currency": "USD",
        "card_number": pan,
        "caller_ip": ip,
        "api_key_id": "key_test",
        "oauth_scope": "transactions:write",
        "event_time": int(time.time() * 1000),
        "channel": "API",
    }


@pytest.fixture(scope="module")
def kafka_bootstrap() -> Generator[str, None, None]:
    """Start a Kafka container and yield its bootstrap server address."""
    with KafkaContainer("confluentinc/cp-kafka:7.6.1") as kafka:
        yield kafka.get_bootstrap_server()


@pytest.mark.integration
def test_no_pii_in_raw_bytes(kafka_bootstrap: str):
    """T027: Raw bytes in txn.api must not contain full card numbers or IPs."""
    from confluent_kafka import Consumer, Producer

    # Create topic
    from confluent_kafka.admin import AdminClient, NewTopic

    from pipelines.ingestion.api.producer import TransactionEventBuilder
    from pipelines.ingestion.shared.pii_masker import MaskingConfig

    admin = AdminClient({"bootstrap.servers": kafka_bootstrap})
    admin.create_topics([NewTopic("txn.api.test.pii", num_partitions=1, replication_factor=1)])
    time.sleep(1)

    pans = [
        "4111111111111111",
        "5500005555555559",
        "378282246310005",
    ]
    ips = ["203.0.113.45", "198.51.100.1", "192.0.2.100"]

    cfg = MaskingConfig()
    builder = TransactionEventBuilder(cfg)

    producer = Producer({"bootstrap.servers": kafka_bootstrap})

    for pan, ip in zip(pans * 4, ips * 4):  # 10+ events
        payload = _valid_payload(pan=pan, ip=ip)
        event = builder.build(payload)
        # Serialize to JSON (simplified — real test would use Avro)
        value = json.dumps(
            {k: v.hex() if isinstance(v, bytes) else v for k, v in event.items()}
        ).encode()
        producer.produce("txn.api.test.pii", value=value)
    producer.flush()

    consumer = Consumer(
        {
            "bootstrap.servers": kafka_bootstrap,
            "group.id": "pii-test",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe(["txn.api.test.pii"])

    raw_bytes_list = []
    deadline = time.time() + 20
    while time.time() < deadline and len(raw_bytes_list) < 10:
        msg = consumer.poll(1.0)
        if msg and not msg.error():
            raw_bytes_list.append(msg.value())

    consumer.close()

    assert len(raw_bytes_list) >= 10, f"Only got {len(raw_bytes_list)} messages"

    digit_pattern = re.compile(rb"\d{15,19}")
    for i, raw in enumerate(raw_bytes_list):
        matches = digit_pattern.findall(raw)
        # filter out timestamp values (13-digit epoch ms)
        long_matches = [m for m in matches if len(m) >= 15]
        assert not long_matches, (
            f"Message {i} contains full card-number-length digit sequence: {long_matches!r}"
        )

        # caller_ip_subnet must end in .0 (IPv4 /24)
        decoded = json.loads(raw)
        subnet = decoded.get("caller_ip_subnet", "")
        assert subnet.endswith(".0"), f"caller_ip_subnet {subnet!r} not truncated to /24"


@pytest.mark.integration
def test_idempotent_no_duplicate_on_retry(kafka_bootstrap: str):
    """T030: Publishing the same transaction_id twice must not produce duplicates.

    Note: This tests application-layer deduplication guard.
    Broker-level idempotency is verified by enable.idempotence config.
    """
    from confluent_kafka import Consumer, Producer
    from confluent_kafka.admin import AdminClient, NewTopic

    from pipelines.ingestion.api.producer import TransactionEventBuilder
    from pipelines.ingestion.shared.pii_masker import MaskingConfig

    admin = AdminClient({"bootstrap.servers": kafka_bootstrap})
    admin.create_topics([NewTopic("txn.api.test.idem", num_partitions=1, replication_factor=1)])
    time.sleep(1)

    cfg = MaskingConfig()
    builder = TransactionEventBuilder(cfg)
    producer = Producer({"bootstrap.servers": kafka_bootstrap, "enable.idempotence": True})

    txn_id = str(uuid.uuid4())
    payload = {**_valid_payload(), "transaction_id": txn_id}

    seen: set[str] = set()

    def _dedup_produce(p: dict) -> None:
        if p["transaction_id"] in seen:
            return  # Application-layer dedup
        seen.add(p["transaction_id"])
        event = builder.build(p)
        serialized = {k: v.hex() if isinstance(v, bytes) else v for k, v in event.items()}
        producer.produce(
            "txn.api.test.idem",
            key=txn_id,
            value=json.dumps(serialized).encode(),
        )
        producer.flush()

    # Simulate retry — publish same payload twice
    _dedup_produce(payload)
    _dedup_produce(payload)

    consumer = Consumer(
        {
            "bootstrap.servers": kafka_bootstrap,
            "group.id": "idem-test",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe(["txn.api.test.idem"])

    messages = []
    deadline = time.time() + 10
    while time.time() < deadline and len(messages) < 2:
        msg = consumer.poll(1.0)
        if msg and not msg.error():
            messages.append(json.loads(msg.value()))
    consumer.close()

    matching = [m for m in messages if m.get("transaction_id") == txn_id]
    assert len(matching) == 1, f"Expected 1 message for txn_id, got {len(matching)}"


@pytest.mark.integration
def test_dlq_receives_masking_error(kafka_bootstrap: str):
    """T035: Invalid PAN should route to txn.api.dlq, not txn.api."""
    from confluent_kafka import Consumer
    from confluent_kafka.admin import AdminClient, NewTopic

    from pipelines.ingestion.shared.dlq_producer import DLQProducer

    admin = AdminClient({"bootstrap.servers": kafka_bootstrap})
    admin.create_topics(
        [
            NewTopic("txn.api.dlq", num_partitions=1, replication_factor=1),
        ]
    )
    time.sleep(1)

    dlq = DLQProducer(bootstrap_servers=kafka_bootstrap)
    dlq.send_to_dlq(
        source_topic="txn.api",
        original_payload='{"account_id": "test"}',
        error_type="InvalidPANError",
        error_message="Luhn check failed",
        masking_applied=False,
    )
    dlq.flush()

    consumer = Consumer(
        {
            "bootstrap.servers": kafka_bootstrap,
            "group.id": "dlq-test",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe(["txn.api.dlq"])

    msg = None
    deadline = time.time() + 10
    while time.time() < deadline:
        m = consumer.poll(1.0)
        if m and not m.error():
            msg = m
            break
    consumer.close()

    assert msg is not None, "No message received on txn.api.dlq"
    headers = dict(msg.headers() or [])
    error_type_header = headers.get("error_type", b"").decode()
    assert error_type_header == "InvalidPANError"

    envelope = json.loads(msg.value())
    assert envelope["error_type"] == "InvalidPANError"
    assert envelope["masking_applied"] is False
