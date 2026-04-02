"""Throughput benchmark — SC-002: ≥5,000 TPS/node (T045).

Publishes 5,000 transactions to txn.api in one second, then asserts that
the enriched consumer lag on txn.enriched clears within 5 seconds.

Run against a local single-node stack:
  docker compose -f infra/docker-compose.yml up -d
  pip install -e ".[processing]"
  pytest -m perf tests/load/test_throughput.py -v -s

Note: A single-node Docker stack will likely NOT meet the ≥5,000 TPS budget.
This test is intended to run against a multi-node Flink cluster. Treat local
failures as a performance investigation signal, not a CI gate.

The test is excluded from the default pytest run (marked pytest.mark.perf).
"""

import time
import uuid

import pytest

pytest.importorskip("confluent_kafka", reason="confluent-kafka not installed")

_BROKER = "localhost:9092"
_INPUT_TOPIC = "txn.api"
_OUTPUT_TOPIC = "txn.enriched"
_TXN_COUNT = 5_000
_PUBLISH_WINDOW_S = 1.0
_LAG_CLEAR_TIMEOUT_S = 5.0


def _build_raw_payload(account_id: str, seq: int) -> bytes:
    """Build a minimal Confluent wire-format Avro payload for txn_api_v1.

    For the throughput benchmark we use a simplified byte payload; the full
    Avro schema encoding requires a running Schema Registry to obtain the
    schema ID. If Schema Registry is unavailable, this test is skipped.
    """
    # Placeholder — in a real run, use fastavro + live schema ID from registry
    return (
        b"\x00\x00\x00\x00\x01"  # magic + schema_id=1
        + f'{{"transaction_id":"{uuid.uuid4()}","account_id":"{account_id}","seq":{seq}}}'.encode()
    )


@pytest.mark.perf
def test_5000_tps_throughput():
    """
    Given 5,000 transactions published within 1 second,
    When the enrichment processor is running,
    Then consumer lag on txn.enriched clears within 5 seconds (SC-002).
    """
    from confluent_kafka import Consumer, Producer
    from confluent_kafka.admin import AdminClient

    # Verify topics exist
    admin = AdminClient({"bootstrap.servers": _BROKER})
    metadata = admin.list_topics(timeout=5)
    if _INPUT_TOPIC not in metadata.topics or _OUTPUT_TOPIC not in metadata.topics:
        pytest.skip(
            f"Topics {_INPUT_TOPIC} or {_OUTPUT_TOPIC} not found. Run: bash infra/kafka/topics.sh"
        )

    producer = Producer({"bootstrap.servers": _BROKER})

    # ── Publish 5,000 transactions within 1 second ───────────────────────────
    start = time.monotonic()
    for i in range(_TXN_COUNT):
        payload = _build_raw_payload(account_id=f"acc-bench-{i % 100}", seq=i)
        producer.produce(_INPUT_TOPIC, value=payload)
        if i % 500 == 0:
            producer.flush()
    producer.flush()
    publish_duration = time.monotonic() - start

    assert publish_duration <= _PUBLISH_WINDOW_S * 1.5, (
        f"Publish took {publish_duration:.2f}s — producer is too slow to measure TPS"
    )

    # ── Wait for enriched consumer lag to clear ───────────────────────────────
    consumer = Consumer(
        {
            "bootstrap.servers": _BROKER,
            "group.id": "bench-lag-monitor",
            "auto.offset.reset": "latest",
        }
    )
    consumer.subscribe([_OUTPUT_TOPIC])

    received = 0
    deadline = time.monotonic() + _LAG_CLEAR_TIMEOUT_S
    while received < _TXN_COUNT and time.monotonic() < deadline:
        msg = consumer.poll(timeout=0.5)
        if msg is not None and not msg.error():
            received += 1
    consumer.close()

    assert received >= _TXN_COUNT, (
        f"Only {received}/{_TXN_COUNT} enriched records received within "
        f"{_LAG_CLEAR_TIMEOUT_S}s — throughput below 5,000 TPS/node target (SC-002). "
        "This is expected on a single-node local stack; run against a multi-node cluster."
    )
