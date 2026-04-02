"""Producer and masking configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field


@dataclass
class ProducerConfig:
    bootstrap_servers: str = field(default_factory=lambda: os.environ["KAFKA_BOOTSTRAP_SERVERS"])
    schema_registry_url: str = field(default_factory=lambda: os.environ["SCHEMA_REGISTRY_URL"])
    acks: str = field(default_factory=lambda: os.environ.get("KAFKA_ACKS", "all"))
    linger_ms: int = field(default_factory=lambda: int(os.environ.get("KAFKA_LINGER_MS", "1")))
    schema_registry_retries: int = field(
        default_factory=lambda: int(os.environ.get("SCHEMA_REGISTRY_RETRIES", "5"))
    )
    ipv4_prefix: int = field(
        default_factory=lambda: int(os.environ.get("MASKING_IPV4_PREFIX", "24"))
    )
    ipv6_prefix: int = field(
        default_factory=lambda: int(os.environ.get("MASKING_IPV6_PREFIX", "64"))
    )
    prometheus_port: int = field(
        default_factory=lambda: int(os.environ.get("PROMETHEUS_PORT", "8001"))
    )
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))

    def librdkafka_config(self) -> dict:
        """Return librdkafka producer config dict."""
        return {
            "bootstrap.servers": self.bootstrap_servers,
            "acks": self.acks,
            "linger.ms": self.linger_ms,
            "queue.buffering.max.ms": 1,
            "batch.size": 65536,
            "enable.idempotence": True,
            "max.in.flight.requests.per.connection": 5,
            "retries": 2147483647,
            "delivery.timeout.ms": 5000,
            "compression.type": "none",
        }
