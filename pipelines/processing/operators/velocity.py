"""VelocityProcessFunction — per-account rolling-window velocity aggregates.

State: MapState<minute_bucket (Long), (count: int, amount_sum: Decimal)>
  - bucket_key = event_time_ms // 60_000
  - O(1) write per event; O(1440 max) read per window scan

See: specs/002-flink-stream-processor/research.md §3
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from pipelines.processing.logging_config import set_transaction_id

logger = logging.getLogger(__name__)

# Window sizes in minutes
_WINDOWS = [
    (1, "1m"),
    (5, "5m"),
    (60, "1h"),
    (1440, "24h"),
]

# 24h + 10s OOO + 31s lateness buffer — bucket retention horizon (ms)
_RETENTION_MS = (24 * 3600 + 41) * 1000

# 7-day idle TTL — event-time timer for account state eviction (ms)
_IDLE_TTL_MS = 7 * 24 * 3600 * 1000

# Flink Long.MIN_VALUE — sentinel meaning "no watermark assigned yet"
_NO_WATERMARK = -9_223_372_036_854_775_808


try:  # pragma: no cover
    from pyflink.common import Types
    from pyflink.common.time import Time
    from pyflink.datastream import KeyedProcessFunction, OutputTag, RuntimeContext
    from pyflink.datastream.state import MapStateDescriptor, StateTtlConfig

    LATE_DLQ_TAG = OutputTag("late_dlq", Types.PICKLED_BYTE_ARRAY())

    class VelocityProcessFunction(KeyedProcessFunction):
        """Keyed on account_id. Maintains MapState<bucket_key, (count, amount)>."""

        def __init__(self, allowed_lateness_ms: int = 30_000) -> None:
            super().__init__()
            self._allowed_lateness_ms = allowed_lateness_ms
            self._pure_state: dict[int, tuple[int, Decimal]] = {}

        def open(self, runtime_context: RuntimeContext) -> None:
            descriptor = MapStateDescriptor(
                "vel_buckets",
                Types.LONG(),
                Types.TUPLE([Types.INT(), Types.BIG_DEC()]),
            )
            ttl_config = (
                StateTtlConfig.new_builder(Time.days(14))
                .set_update_type(StateTtlConfig.UpdateType.OnCreateAndWrite)
                .build()
            )
            descriptor.enable_time_to_live(ttl_config)
            self._buckets = runtime_context.get_map_state(descriptor)

        def process_element(self, txn, ctx):
            set_transaction_id(getattr(txn, "transaction_id", None))

            event_time_ms: int = ctx.timestamp()
            current_watermark: int = ctx.timer_service().current_watermark()

            # FR-014: late events beyond allowed lateness → DLQ
            if (
                current_watermark != _NO_WATERMARK
                and event_time_ms < current_watermark - self._allowed_lateness_ms
            ):
                from pipelines.processing.metrics import dlq_events_total
                from pipelines.processing.shared.dlq_sink import (
                    build_dlq_record,
                )

                dlq_events_total.labels(error_type="LATE_EVENT_BEYOND_ALLOWED_LATENESS").inc()
                dlq_record = build_dlq_record(
                    source_topic="txn.api",
                    source_partition=0,
                    source_offset=0,
                    original_payload_bytes=b"",
                    error_type="LATE_EVENT_BEYOND_ALLOWED_LATENESS",
                    error_message=(
                        f"event_time={event_time_ms} is "
                        f"{current_watermark - event_time_ms}ms behind "
                        f"watermark (allowed="
                        f"{self._allowed_lateness_ms}ms)"
                    ),
                    transaction_id=getattr(txn, "transaction_id", None),
                    watermark_at_rejection=current_watermark,
                    event_time_at_rejection=event_time_ms,
                )
                ctx.output(LATE_DLQ_TAG, dlq_record)
                return

            bucket_key: int = event_time_ms // 60_000
            amount = Decimal(str(txn.amount)) if not isinstance(txn.amount, Decimal) else txn.amount

            existing = self._buckets.get(bucket_key)
            if existing is None:
                existing = (0, Decimal("0"))
            self._buckets.put(bucket_key, (existing[0] + 1, existing[1] + amount))

            velocity = _compute_velocity(self._buckets, bucket_key)

            ctx.timer_service().register_event_time_timer(event_time_ms + _RETENTION_MS)
            ctx.timer_service().register_event_time_timer(event_time_ms + _IDLE_TTL_MS)

            try:
                from pipelines.processing.metrics import (
                    last_watermark_advance_epoch,
                )

                last_watermark_advance_epoch.set(time.time())
            except Exception:
                pass

            yield txn, velocity

        def on_timer(self, timestamp: int, ctx) -> None:
            cutoff_bucket = (timestamp - _RETENTION_MS) // 60_000
            stale = [bk for bk in self._buckets.keys() if bk < cutoff_bucket]
            for bk in stale:
                self._buckets.remove(bk)
            if not list(self._buckets.keys()):
                self._buckets.clear()

        def process_element_pure(self, event_time_ms: int, amount: Decimal) -> dict:
            """Test-facing method: update in-memory state and return velocity dict."""
            bucket_key = event_time_ms // 60_000
            existing = self._pure_state.get(bucket_key, (0, Decimal("0")))
            self._pure_state[bucket_key] = (
                existing[0] + 1,
                existing[1] + amount,
            )
            return _compute_velocity_from_dict(self._pure_state, bucket_key)

except ImportError:
    # pyflink not installed — plain-Python fallback for unit tests

    class VelocityProcessFunction:  # type: ignore[no-redef]  # pragma: no cover
        """Pure-Python stand-in used by unit tests."""

        def __init__(self, allowed_lateness_ms: int = 30_000) -> None:
            self._allowed_lateness_ms = allowed_lateness_ms
            self._state: dict[int, tuple[int, Decimal]] = {}

        def process_element_pure(self, event_time_ms: int, amount: Decimal) -> dict:
            """Test-facing method: update state and return velocity dict."""
            bucket_key = event_time_ms // 60_000
            existing = self._state.get(bucket_key, (0, Decimal("0")))
            self._state[bucket_key] = (
                existing[0] + 1,
                existing[1] + amount,
            )
            return _compute_velocity_from_dict(self._state, bucket_key)


def _compute_velocity(buckets, current_bucket: int) -> dict:  # pragma: no cover
    """Scan MapState buckets (pyflink MapState) — requires pyflink runtime."""
    velocity: dict[str, object] = {}
    all_buckets = {bk: v for bk, v in buckets.items()}
    for mins, label in _WINDOWS:
        cutoff = current_bucket - mins
        count, total = 0, Decimal("0")
        for bk, (c, a) in all_buckets.items():
            if bk >= cutoff:
                count += c
                total += a if isinstance(a, Decimal) else Decimal(str(a))
        velocity[f"vel_count_{label}"] = count
        velocity[f"vel_amount_{label}"] = total
    return velocity


def _compute_velocity_from_dict(state: dict[int, tuple[int, Decimal]], current_bucket: int) -> dict:
    """Pure-Python version for unit tests."""
    velocity: dict[str, object] = {}
    for mins, label in _WINDOWS:
        cutoff = current_bucket - mins
        count, total = 0, Decimal("0")
        for bk, (c, a) in state.items():
            if bk >= cutoff:
                count += c
                total += a
        velocity[f"vel_count_{label}"] = count
        velocity[f"vel_amount_{label}"] = total
    return velocity
