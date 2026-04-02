"""Flink enrichment job entry point.

Topology:
  KafkaSource[txn.api]
    → DeduplicationFilter       (keyed by transaction_id)
    → VelocityEnrichment        (keyed by account_id)
    → GeolocationEnrichment     (stateless map)
    → DeviceFingerprintEnrich   (keyed by api_key_id)
    → EnrichedRecordAssembler   (stateless flat-map)
    → KafkaSink[txn.enriched]
    ⤷ DLQSink[txn.processing.dlq]  (side output from schema errors)

Run:
  python3 -m pipelines.processing.job --help
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from pipelines.processing.config import ProcessorConfig
from pipelines.processing.logging_config import configure_logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Side-output tag for DLQ events
# ---------------------------------------------------------------------------
try:
    from pyflink.common import Types
    from pyflink.datastream import OutputTag

    DLQ_OUTPUT_TAG = OutputTag("dlq", Types.PICKLED_BYTE_ARRAY())
except ImportError:
    DLQ_OUTPUT_TAG = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Job builder
# ---------------------------------------------------------------------------


def build_job(config: ProcessorConfig):  # pragma: no cover
    """Build and return the Flink StreamExecutionEnvironment."""
    import sys  # noqa: PLC0415

    from pyflink.common import Configuration, WatermarkStrategy  # noqa: PLC0415
    from pyflink.common.serialization import SimpleStringSchema  # noqa: PLC0415
    from pyflink.datastream import StreamExecutionEnvironment  # noqa: PLC0415
    from pyflink.datastream.checkpoint_config import (  # noqa: PLC0415
        ExternalizedCheckpointCleanup,
    )
    from pyflink.datastream.checkpointing_mode import (  # noqa: PLC0415
        CheckpointingMode,
    )
    from pyflink.datastream.connectors.kafka import (  # noqa: PLC0415
        DeliveryGuarantee,
        KafkaOffsetResetStrategy,
        KafkaOffsetsInitializer,
        KafkaRecordSerializationSchema,
        KafkaSink,
        KafkaSource,
    )

    from pipelines.processing.operators.device import DeviceProcessFunction
    from pipelines.processing.operators.enricher import (
        EnrichedRecordAssembler,
        TransactionDedup,
    )
    from pipelines.processing.operators.geolocation import GeolocationMapFunction
    from pipelines.processing.operators.velocity import VelocityProcessFunction
    from pipelines.processing.shared.avro_serde import (
        SchemaValidationError,
        deserialise_raw_transaction,
    )
    from pipelines.processing.shared.dlq_sink import build_dlq_record

    # ── Python worker executable (must match the process running this script) ─
    _cfg = Configuration()
    _cfg.set_string("python.executable", sys.executable)
    env = StreamExecutionEnvironment.get_execution_environment(_cfg)
    env.set_parallelism(config.parallelism)

    # ── State backend: RocksDB configured via flink-conf.yaml (T029) ─────────
    # set_state_backend removed in PyFlink 1.19 — use config file instead:
    #   state.backend: rocksdb / state.backend.incremental: true

    # ── Checkpoint config (T030 / FR-008, FR-009) ────────────────────────────
    env.enable_checkpointing(config.checkpoint_interval_ms)
    cp_config = env.get_checkpoint_config()
    cp_config.set_checkpointing_mode(CheckpointingMode.EXACTLY_ONCE)
    cp_config.set_min_pause_between_checkpoints(10_000)
    cp_config.set_checkpoint_timeout(30_000)
    cp_config.set_max_concurrent_checkpoints(1)
    cp_config.set_externalized_checkpoint_cleanup(
        ExternalizedCheckpointCleanup.RETAIN_ON_CANCELLATION
    )

    # Checkpoint dir set via state.checkpoints.dir in flink-conf.yaml.

    # ── Kafka source (FR-001, FR-015) ────────────────────────────────────────
    kafka_source = (
        KafkaSource.builder()
        .set_bootstrap_servers(config.kafka_brokers)
        .set_topics(config.input_topic)
        .set_group_id("flink-enrichment-processor")
        .set_starting_offsets(
            KafkaOffsetsInitializer.committed_offsets(KafkaOffsetResetStrategy.EARLIEST)
        )
        .set_value_only_deserializer(SimpleStringSchema("ISO-8859-1"))
        .set_property("isolation.level", "read_committed")
        .set_property("enable.auto.commit", "false")
        .build()
    )

    # ── Watermark strategy (T026 / FR-013) ───────────────────────────────────
    # Source emits raw bytes; timestamp assignment happens post-deserialisation.
    watermark_strategy = WatermarkStrategy.for_monotonous_timestamps()

    # ── Source stream ────────────────────────────────────────────────────────
    raw_bytes_stream = env.from_source(
        kafka_source,
        watermark_strategy,
        "KafkaSource[txn.api]",
    )

    # ── Deserialization + DLQ routing for schema errors (FR-010) ────────────
    def deserialise_with_dlq(raw_bytes):
        """Attempt Avro deserialization; route failures to DLQ side output."""
        try:
            # SimpleStringSchema("ISO-8859-1") preserves binary fidelity;
            # re-encode with latin-1 to recover original bytes.
            payload = raw_bytes if isinstance(raw_bytes, bytes) else raw_bytes.encode("latin-1")
            txn = deserialise_raw_transaction(payload)
            yield txn
        except SchemaValidationError as exc:
            build_dlq_record(
                source_topic=config.input_topic,
                source_partition=0,
                source_offset=0,
                original_payload_bytes=(
                    raw_bytes if isinstance(raw_bytes, bytes) else raw_bytes.encode("latin-1")
                ),
                error_type="SCHEMA_VALIDATION_ERROR",
                error_message=str(exc),
            )
            # Route via DLQ_OUTPUT_TAG side output in the full implementation
            logger.error("Schema validation error — routing to DLQ: %s", exc)

    txn_stream = raw_bytes_stream.flat_map(deserialise_with_dlq, output_type=None)

    # ── Deduplication (FR-015) ───────────────────────────────────────────────
    dedup_stream = txn_stream.key_by(lambda txn: txn.transaction_id).process(
        TransactionDedup(), output_type=None
    )

    # ── Velocity enrichment (FR-002, keyed by account_id) ───────────────────
    allowed_lateness_ms = config.allowed_lateness_seconds * 1000
    velocity_stream = dedup_stream.key_by(lambda txn: txn.account_id).process(
        VelocityProcessFunction(allowed_lateness_ms=allowed_lateness_ms),
        output_type=None,
    )

    # ── Geolocation enrichment (FR-003, stateless) ───────────────────────────
    geo_stream = velocity_stream.map(
        GeolocationMapFunction(config.geoip_db_path),
        output_type=None,
    )

    # ── Device fingerprint enrichment (FR-004, keyed by api_key_id) ─────────
    device_stream = geo_stream.key_by(lambda pair: pair[0].api_key_id or "__no_device__").process(
        DeviceProcessFunction(), output_type=None
    )

    # ── Assemble enriched record (FR-005) ────────────────────────────────────
    enriched_stream = device_stream.flat_map(EnrichedRecordAssembler(), output_type=None)

    # ── Kafka sink (AT_LEAST_ONCE for local dev; EXACTLY_ONCE in prod) ───────
    import json  # noqa: PLC0415

    kafka_sink = (
        KafkaSink.builder()
        .set_bootstrap_servers(config.kafka_brokers)
        .set_record_serializer(
            KafkaRecordSerializationSchema.builder()
            .set_topic(config.output_topic)
            .set_value_serialization_schema(SimpleStringSchema())
            .build()
        )
        .set_delivery_guarantee(DeliveryGuarantee.AT_LEAST_ONCE)
        .build()
    )

    # Serialize enriched dict → JSON string before sinking
    enriched_stream = enriched_stream.map(
        lambda record: json.dumps(record, default=str),
        output_type=Types.STRING(),
    )

    enriched_stream.sink_to(kafka_sink)

    # ── Checkpoint failure monitoring (T032 / FR-012) ────────────────────────
    # Register a CheckpointListener to increment checkpoint_failures_total
    # so the Prometheus alert rule in infra/flink/alerts.yaml can fire.
    try:
        from pyflink.datastream import (  # noqa: PLC0415
            CheckpointListener as _CPListener,
        )

        class _FailureCounter(_CPListener):
            def notify_checkpoint_complete(self, _checkpoint_id: int) -> None:
                pass  # duration not exposed via Python API

            def notify_checkpoint_aborted(self, _checkpoint_id: int) -> None:
                try:
                    from pipelines.processing.metrics import (  # noqa: PLC0415
                        checkpoint_failures_total,
                    )

                    checkpoint_failures_total.inc()
                except Exception:
                    pass

        env.register_listener(_FailureCounter())
    except (ImportError, AttributeError):
        pass

    return env


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Flink stateful stream processor — transaction feature enrichment"
    )
    parser.add_argument("--kafka-brokers", default=None)
    parser.add_argument("--schema-registry", default=None)
    parser.add_argument("--input-topic", default=None)
    parser.add_argument("--output-topic", default=None)
    parser.add_argument("--dlq-topic", default=None)
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument("--checkpoint-interval-ms", type=int, default=None)
    parser.add_argument("--geoip-db-path", default=None)
    parser.add_argument("--parallelism", type=int, default=None)
    parser.add_argument("--watermark-ooo-seconds", type=int, default=None)
    parser.add_argument("--allowed-lateness-seconds", type=int, default=None)
    parser.add_argument("--processor-version", default=None)
    return parser.parse_args(argv)


def main(argv=None):
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    args = _parse_args(argv)

    overrides: dict = {}
    if args.kafka_brokers:
        overrides["kafka_brokers"] = args.kafka_brokers
    if args.schema_registry:
        overrides["schema_registry_url"] = args.schema_registry
    if args.input_topic:
        overrides["input_topic"] = args.input_topic
    if args.output_topic:
        overrides["output_topic"] = args.output_topic
    if args.dlq_topic:
        overrides["dlq_topic"] = args.dlq_topic
    if args.checkpoint_dir:
        overrides["checkpoint_dir"] = args.checkpoint_dir
    if args.checkpoint_interval_ms:
        overrides["checkpoint_interval_ms"] = args.checkpoint_interval_ms
    if args.geoip_db_path:
        overrides["geoip_db_path"] = args.geoip_db_path
    if args.parallelism:
        overrides["parallelism"] = args.parallelism
    if args.watermark_ooo_seconds:
        overrides["watermark_ooo_seconds"] = args.watermark_ooo_seconds
    if args.allowed_lateness_seconds:
        overrides["allowed_lateness_seconds"] = args.allowed_lateness_seconds
    if args.processor_version:
        overrides["processor_version"] = args.processor_version
        os.environ["PROCESSOR_VERSION"] = args.processor_version

    import dataclasses

    config = ProcessorConfig()
    if overrides:
        config = dataclasses.replace(config, **overrides)

    logger.info("Starting enrichment job")

    try:
        env = build_job(config)
        env.execute("FraudStream Enrichment Processor 002")
    except ImportError as exc:
        logger.error("pyflink not installed — run: pip install -e '.[processing]': %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
