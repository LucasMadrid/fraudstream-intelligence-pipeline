"""EnrichedRecordAssembler and TransactionDedup operators."""

from __future__ import annotations

import logging
import os
import time

from pipelines.processing.logging_config import set_transaction_id

logger = logging.getLogger(__name__)

_PROCESSOR_VERSION = os.environ.get("PROCESSOR_VERSION", "002-stream-processor@1.0.0")


try:  # pragma: no cover
    from pyflink.common import Types
    from pyflink.common.time import Time
    from pyflink.datastream import KeyedProcessFunction, RuntimeContext
    from pyflink.datastream.functions import FlatMapFunction
    from pyflink.datastream.state import StateTtlConfig, ValueStateDescriptor

    class EnrichedRecordAssembler(FlatMapFunction):
        """Stateless: assembles the final EnrichedTransactionEvent."""

        def flat_map(self, value):
            txn, velocity_dict, geo_dict, device_dict = value
            set_transaction_id(getattr(txn, "transaction_id", None))
            enrichment_time = int(time.time() * 1000)
            enrichment_latency_ms = max(0, enrichment_time - txn.processing_time)
            try:
                from pipelines.processing.metrics import (
                    enrichment_latency_ms as lat_metric,
                )

                lat_metric.observe(enrichment_latency_ms)
            except Exception:
                pass
            yield _assemble_record(
                txn,
                velocity_dict,
                geo_dict,
                device_dict,
                enrichment_time,
                enrichment_latency_ms,
            )

        def assemble(self, txn, velocity_dict, geo_dict, device_dict) -> dict:
            """Test-friendly wrapper around _assemble_record."""
            enrichment_time = int(time.time() * 1000)
            enrichment_latency_ms = max(
                0,
                enrichment_time - getattr(txn, "processing_time", enrichment_time),
            )
            return _assemble_record(
                txn,
                velocity_dict,
                geo_dict,
                device_dict,
                enrichment_time,
                enrichment_latency_ms,
            )

    class TransactionDedup(KeyedProcessFunction):
        """Keyed on transaction_id. Drops duplicates within 48h TTL."""

        def __init__(self) -> None:
            super().__init__()
            self._seen_dict: dict[str, int] = {}  # for process_element_pure

        def open(self, runtime_context: RuntimeContext) -> None:
            descriptor = ValueStateDescriptor("dedup_seen_at", Types.LONG())
            ttl_config = (
                StateTtlConfig.new_builder(Time.hours(48))
                .set_update_type(StateTtlConfig.UpdateType.OnCreateAndWrite)
                .build()
            )
            descriptor.enable_time_to_live(ttl_config)
            self._seen = runtime_context.get_state(descriptor)

        def process_element(self, txn, ctx):
            set_transaction_id(getattr(txn, "transaction_id", None))
            if self._seen.value() is not None:
                try:
                    from pipelines.processing.metrics import (
                        dedup_skipped_total,
                    )

                    dedup_skipped_total.inc()
                except Exception:
                    pass
                return
            self._seen.update(ctx.timestamp())
            yield txn

        def process_element_pure(self, txn_id: str, event_time_ms: int) -> bool:
            """Test-friendly: returns True if not a duplicate."""
            if txn_id in self._seen_dict:
                return False
            self._seen_dict[txn_id] = event_time_ms
            return True

except ImportError:
    # pyflink not installed — plain-Python fallback for unit tests

    class EnrichedRecordAssembler:  # type: ignore[no-redef]  # pragma: no cover
        """Plain-Python stand-in for unit tests."""

        def assemble(self, txn, velocity_dict, geo_dict, device_dict) -> dict:
            enrichment_time = int(time.time() * 1000)
            enrichment_latency_ms = max(
                0,
                enrichment_time - getattr(txn, "processing_time", enrichment_time),
            )
            return _assemble_record(
                txn,
                velocity_dict,
                geo_dict,
                device_dict,
                enrichment_time,
                enrichment_latency_ms,
            )

    class TransactionDedup:  # type: ignore[no-redef]  # pragma: no cover
        """Plain-Python stand-in for unit tests."""

        def __init__(self) -> None:
            self._seen: dict[str, int] = {}

        def process_element_pure(self, txn_id: str, event_time_ms: int) -> bool:
            """Returns True if not a duplicate."""
            if txn_id in self._seen:
                return False
            self._seen[txn_id] = event_time_ms
            return True


def _assemble_record(
    txn,
    velocity_dict,
    geo_dict,
    device_dict,
    enrichment_time,
    enrichment_latency_ms,
) -> dict:
    """Build the 36-field EnrichedTransactionEvent dict."""
    return {
        "transaction_id": txn.transaction_id,
        "account_id": txn.account_id,
        "merchant_id": txn.merchant_id,
        "amount": txn.amount,
        "currency": txn.currency,
        "event_time": txn.event_time,
        "processing_time": txn.processing_time,
        "channel": txn.channel,
        "card_bin": txn.card_bin,
        "card_last4": txn.card_last4,
        "caller_ip_subnet": txn.caller_ip_subnet,
        "api_key_id": txn.api_key_id,
        "oauth_scope": txn.oauth_scope,
        "geo_lat": getattr(txn, "geo_lat", None),
        "geo_lon": getattr(txn, "geo_lon", None),
        "masking_lib_version": txn.masking_lib_version,
        "vel_count_1m": velocity_dict["vel_count_1m"],
        "vel_amount_1m": velocity_dict["vel_amount_1m"],
        "vel_count_5m": velocity_dict["vel_count_5m"],
        "vel_amount_5m": velocity_dict["vel_amount_5m"],
        "vel_count_1h": velocity_dict["vel_count_1h"],
        "vel_amount_1h": velocity_dict["vel_amount_1h"],
        "vel_count_24h": velocity_dict["vel_count_24h"],
        "vel_amount_24h": velocity_dict["vel_amount_24h"],
        "geo_country": geo_dict.get("geo_country"),
        "geo_city": geo_dict.get("geo_city"),
        "geo_network_class": geo_dict.get("geo_network_class"),
        "geo_confidence": geo_dict.get("geo_confidence"),
        "device_first_seen": device_dict.get("device_first_seen"),
        "device_txn_count": device_dict.get("device_txn_count"),
        "device_known_fraud": device_dict.get("device_known_fraud"),
        "enrichment_time": enrichment_time,
        "enrichment_latency_ms": enrichment_latency_ms,
        "processor_version": _PROCESSOR_VERSION,
        "schema_version": "1",
    }
