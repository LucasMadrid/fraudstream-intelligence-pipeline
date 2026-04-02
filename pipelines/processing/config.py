"""ProcessorConfig — all Flink job settings, env-var driven."""

import os
from dataclasses import dataclass, field


@dataclass
class ProcessorConfig:
    kafka_brokers: str = field(
        default_factory=lambda: os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    )
    schema_registry_url: str = field(
        default_factory=lambda: os.environ.get("SCHEMA_REGISTRY_URL", "http://localhost:8081")
    )
    input_topic: str = field(default_factory=lambda: os.environ.get("INPUT_TOPIC", "txn.api"))
    output_topic: str = field(
        default_factory=lambda: os.environ.get("OUTPUT_TOPIC", "txn.enriched")
    )
    dlq_topic: str = field(
        default_factory=lambda: os.environ.get("DLQ_TOPIC", "txn.processing.dlq")
    )
    checkpoint_dir: str = field(
        default_factory=lambda: os.environ.get(
            "CHECKPOINT_DIR", "s3://flink-checkpoints/002-stream-processor"
        )
    )
    checkpoint_interval_ms: int = field(
        default_factory=lambda: int(os.environ.get("CHECKPOINT_INTERVAL_MS", "60000"))
    )
    geoip_db_path: str = field(
        default_factory=lambda: os.environ.get(
            "GEOIP_DB_PATH", "/opt/flink/geoip/GeoLite2-City.mmdb"
        )
    )
    parallelism: int = field(default_factory=lambda: int(os.environ.get("PARALLELISM", "1")))
    watermark_ooo_seconds: int = field(
        default_factory=lambda: int(os.environ.get("WATERMARK_OOO_SECONDS", "10"))
    )
    allowed_lateness_seconds: int = field(
        default_factory=lambda: int(os.environ.get("ALLOWED_LATENESS_SECONDS", "30"))
    )
    # S3/MinIO credentials for checkpoint storage
    s3_endpoint: str = field(
        default_factory=lambda: os.environ.get("S3_ENDPOINT", "http://minio:9000")
    )
    s3_access_key: str = field(
        default_factory=lambda: os.environ.get("S3_ACCESS_KEY", "minioadmin")
    )
    s3_secret_key: str = field(
        default_factory=lambda: os.environ.get("S3_SECRET_KEY", "minioadmin")
    )
    processor_version: str = field(
        default_factory=lambda: os.environ.get("PROCESSOR_VERSION", "002-stream-processor@1.0.0")
    )
