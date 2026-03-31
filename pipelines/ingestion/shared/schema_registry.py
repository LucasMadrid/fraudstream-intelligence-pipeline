"""Schema Registry client wrapper with startup retry and schema caching."""

import logging
import time
from pathlib import Path

from confluent_kafka.schema_registry import SchemaRegistryClient as _ConfluentSRClient
from confluent_kafka.schema_registry.avro import AvroSerializer

logger = logging.getLogger(__name__)


def connect_with_retry(
    url: str,
    retries: int = 5,
    base_delay: float = 1.0,
) -> "_SchemaRegistryWrapper":
    """Connect to Schema Registry with exponential backoff.

    Raises RuntimeError after all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            client = _ConfluentSRClient({"url": url})
            # Probe liveness — list_subjects() is a cheap GET
            client.get_subjects()
            wrapper = _SchemaRegistryWrapper(client)
            logger.info(
                "schema_registry_connected",
                extra={"url": url, "attempt": attempt + 1},
            )
            return wrapper
        except Exception as exc:
            last_exc = exc
            delay = base_delay * (2**attempt)
            logger.warning(
                "schema_registry_unavailable",
                extra={"url": url, "attempt": attempt + 1, "retry_in_s": delay},
            )
            if attempt < retries - 1:
                time.sleep(delay)

    raise RuntimeError(
        f"Schema Registry unreachable after {retries} attempts: {last_exc}"
    ) from last_exc


class _SchemaRegistryWrapper:
    """Thin wrapper around ConfluentSRClient with schema-ID caching."""

    def __init__(self, client: _ConfluentSRClient) -> None:
        self._client = client
        self._serializer_cache: dict[str, AvroSerializer] = {}

    def get_serializer(self, subject: str, schema_str: str) -> AvroSerializer:
        """Return a cached AvroSerializer for the given subject.

        auto.register.schemas is always False — schema must be pre-registered.
        """
        if subject not in self._serializer_cache:
            serializer = AvroSerializer(
                self._client,
                schema_str,
                conf={"auto.register.schemas": False},
            )
            self._serializer_cache[subject] = serializer
            logger.info(
                "schema_serializer_created",
                extra={"subject": subject},
            )
        return self._serializer_cache[subject]

    def get_subjects(self) -> list[str]:
        return self._client.get_subjects()


def load_schema_str(schema_file: str | Path) -> str:
    """Load an Avro schema file and return it as a JSON string."""
    return Path(schema_file).read_text(encoding="utf-8")
