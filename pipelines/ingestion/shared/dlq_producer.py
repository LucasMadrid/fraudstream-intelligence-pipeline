"""Dead-letter queue producer — separate Kafka Producer instance."""

import json
import logging
import socket
import time
import uuid

from confluent_kafka import Producer

logger = logging.getLogger(__name__)

DLQ_TOPIC = "txn.api.dlq"


class DLQProducer:
    """Writes DLQEnvelope messages to txn.api.dlq.

    Uses acks=1 and linger.ms=5 to avoid blocking on broker issues.
    Never calls flush() inside a delivery callback.
    """

    def __init__(self, bootstrap_servers: str) -> None:
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers,
                "acks": 1,
                "linger.ms": 5,
                "client.id": f"dlq-producer-{socket.gethostname()}",
            }
        )

    def send_to_dlq(
        self,
        source_topic: str,
        original_payload: str,
        error_type: str,
        error_message: str,
        masking_applied: bool,
    ) -> None:
        """Serialise a DLQEnvelope to JSON and produce to txn.api.dlq."""
        envelope = {
            "dlq_id": str(uuid.uuid4()),
            "source_topic": source_topic,
            "original_payload": original_payload,
            "error_type": error_type,
            "error_message": error_message,
            "failed_at": int(time.time() * 1000),
            "producer_host": socket.gethostname(),
            "masking_applied": masking_applied,
        }
        value = json.dumps(envelope).encode("utf-8")
        self._producer.produce(
            topic=DLQ_TOPIC,
            value=value,
            headers={"error_type": error_type},
        )
        # poll(0) services the delivery queue without blocking
        self._producer.poll(0)

        logger.warning(
            "dlq_message_sent",
            extra={
                "component": "dlq-producer",
                "dlq_id": envelope["dlq_id"],
                "source_topic": source_topic,
                "error_type": error_type,
                "masking_applied": masking_applied,
            },
        )

    def flush(self, timeout: float = 5.0) -> None:
        self._producer.flush(timeout)
