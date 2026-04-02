"""DeviceProcessFunction — per-device profile accumulation.

Keyed on api_key_id (v1 proxy for device_id — v2 field deferred).
State: ValueState<DeviceProfileState>

See: specs/002-flink-stream-processor/data-model.md §5
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pipelines.processing.logging_config import set_transaction_id

logger = logging.getLogger(__name__)

_IDLE_TTL_MS = 7 * 24 * 3600 * 1000  # 7-day event-time idle TTL


@dataclass(frozen=True)
class DeviceProfileState:
    first_seen_ms: int  # epoch ms of first transaction from this device
    txn_count: int  # lifetime transaction count
    known_fraud: bool  # always False in v1 (fraud label loop not wired)
    last_seen_ms: int  # epoch ms of most recent transaction (TTL timer)


_NULL_DEVICE_FIELDS: dict = {
    "device_first_seen": None,
    "device_txn_count": None,
    "device_known_fraud": None,
}


try:  # pragma: no cover
    from pyflink.common import Types
    from pyflink.common.time import Time
    from pyflink.datastream import KeyedProcessFunction, RuntimeContext
    from pyflink.datastream.state import (
        StateTtlConfig,
        ValueStateDescriptor,
    )

    class DeviceProcessFunction(KeyedProcessFunction):
        """Keyed on api_key_id. Maintains ValueState<DeviceProfileState>."""

        def __init__(self) -> None:
            super().__init__()
            self._pure_state: dict[str, DeviceProfileState] = {}

        def open(self, runtime_context: RuntimeContext) -> None:
            descriptor = ValueStateDescriptor(
                "device_profile",
                Types.PICKLED_BYTE_ARRAY(),
            )
            ttl_config = (
                StateTtlConfig.new_builder(Time.days(14))
                .set_update_type(StateTtlConfig.UpdateType.OnCreateAndWrite)
                .build()
            )
            descriptor.enable_time_to_live(ttl_config)
            self._state = runtime_context.get_state(descriptor)

        def process_element(self, value, ctx):
            txn, velocity, geo = value
            set_transaction_id(getattr(txn, "transaction_id", None))
            api_key_id = getattr(txn, "api_key_id", None)

            if not api_key_id:
                yield txn, velocity, geo, dict(_NULL_DEVICE_FIELDS)
                return

            event_time_ms: int = ctx.timestamp()
            current: DeviceProfileState | None = self._state.value()

            if current is None:
                updated = DeviceProfileState(
                    first_seen_ms=event_time_ms,
                    txn_count=1,
                    known_fraud=False,
                    last_seen_ms=event_time_ms,
                )
            else:
                updated = DeviceProfileState(
                    first_seen_ms=current.first_seen_ms,
                    txn_count=current.txn_count + 1,
                    known_fraud=current.known_fraud,
                    last_seen_ms=event_time_ms,
                )

            self._state.update(updated)
            ctx.timer_service().register_event_time_timer(event_time_ms + _IDLE_TTL_MS)
            yield (
                txn,
                velocity,
                geo,
                {
                    "device_first_seen": updated.first_seen_ms,
                    "device_txn_count": updated.txn_count,
                    "device_known_fraud": updated.known_fraud,
                },
            )

        def on_timer(self, _timestamp: int, _ctx) -> None:
            self._state.clear()

        def process_element_pure(self, api_key_id: str | None, event_time_ms: int) -> dict:
            """Test-friendly: update in-memory state, return device fields dict."""
            if not api_key_id:
                return dict(_NULL_DEVICE_FIELDS)

            current = self._pure_state.get(api_key_id)
            if current is None:
                updated = DeviceProfileState(
                    first_seen_ms=event_time_ms,
                    txn_count=1,
                    known_fraud=False,
                    last_seen_ms=event_time_ms,
                )
            else:
                updated = DeviceProfileState(
                    first_seen_ms=current.first_seen_ms,
                    txn_count=current.txn_count + 1,
                    known_fraud=current.known_fraud,
                    last_seen_ms=event_time_ms,
                )
            self._pure_state[api_key_id] = updated
            return {
                "device_first_seen": updated.first_seen_ms,
                "device_txn_count": updated.txn_count,
                "device_known_fraud": updated.known_fraud,
            }

except ImportError:
    # pyflink not installed — plain-Python fallback for unit tests

    class DeviceProcessFunction:  # type: ignore[no-redef]  # pragma: no cover
        """Plain-Python stand-in for unit tests."""

        def __init__(self) -> None:
            self._state: dict[str, DeviceProfileState] = {}

        def process_element_pure(self, api_key_id: str | None, event_time_ms: int) -> dict:
            """Update state and return device fields dict."""
            if not api_key_id:
                return dict(_NULL_DEVICE_FIELDS)

            current = self._state.get(api_key_id)
            if current is None:
                updated = DeviceProfileState(
                    first_seen_ms=event_time_ms,
                    txn_count=1,
                    known_fraud=False,
                    last_seen_ms=event_time_ms,
                )
            else:
                updated = DeviceProfileState(
                    first_seen_ms=current.first_seen_ms,
                    txn_count=current.txn_count + 1,
                    known_fraud=current.known_fraud,
                    last_seen_ms=event_time_ms,
                )
            self._state[api_key_id] = updated
            return {
                "device_first_seen": updated.first_seen_ms,
                "device_txn_count": updated.txn_count,
                "device_known_fraud": updated.known_fraud,
            }
