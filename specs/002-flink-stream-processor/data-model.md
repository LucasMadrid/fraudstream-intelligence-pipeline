# Data Model: Stateful Stream Processor — Transaction Feature Enrichment

**Branch**: `002-flink-stream-processor` | **Date**: 2026-03-31

---

## 1. Entity Overview

| Entity | Storage | Key | TTL |
|--------|---------|-----|-----|
| RawTransaction | Kafka `txn.api` (input) | `account_id` (partition) | Kafka topic retention |
| VelocityAggregate | Flink RocksDB `ListState` | `account_id` | 7d event-time + 14d processing-time fallback |
| GeolocationContext | Flink in-memory LRU cache | `/24` subnet string | 24h LRU eviction |
| DeviceProfile | Flink RocksDB `MapState` | `device_id` | 7d event-time + 14d processing-time fallback |
| TransactionDedup | Flink RocksDB `ValueState<Long>` | `transaction_id` | 48h processing-time |
| EnrichedTransaction | Kafka `txn.enriched` (output) | `account_id` (partition) | Kafka topic retention |
| DeadLetterRecord | Kafka `txn.processing.dlq` | `transaction_id` (if available) | Kafka topic retention |
| ProcessorCheckpoint | MinIO `s3://flink-checkpoints/` | checkpoint sequence ID | 5 retained (configurable) |

---

## 2. RawTransaction (input — from feature 001)

Consumed from `txn.api`. Schema: `txn_api_v1` (see `specs/001-kafka-ingestion-pipeline/contracts/txn-api-v1.avsc`).

**Fields used by the processor:**

| Field | Type | Use |
|-------|------|-----|
| `transaction_id` | string (UUID v4) | Deduplication key; forwarded to output |
| `account_id` | string | Velocity aggregate key; Kafka partition key |
| `event_time` | long (timestamp-millis) | Watermark assignment; window boundary computation |
| `amount` | bytes (decimal 18,4) | Velocity amount summation |
| `caller_ip_subnet` | string (/24 subnet) | Geolocation lookup input |
| `device_id` (implied) | string | Device profile key — present in schema as `api_key_id` context; see note below |
| All other fields | various | Forwarded to `EnrichedTransaction` unchanged |

> **Note on device_id:** The `txn_api_v1` schema exposes `api_key_id` as the API channel caller identifier. For device fingerprinting in the API channel, `api_key_id` serves as the device proxy identifier in v1 (a dedicated `device_id` field is deferred to schema v2). This is documented in the `enriched-txn-v1.avsc` contract.

---

## 3. VelocityAggregate (Flink keyed state)

**State type:** `MapState<Long, Tuple2<Integer, BigDecimal>>` — keyed by minute bucket (`event_time_ms // 60_000`), value = `(count, amount_sum)` for that minute.
**Key:** `account_id` (Flink keyed stream).
**Why `MapState<bucket>` over `ListState<(ts, amount)>`:** O(1) write per event (increment one bucket) vs O(N) scan for `ListState`. Window computation scans at most 1,440 buckets (24h / 1min) — bounded regardless of transaction volume. See [research.md §3](research.md#3-velocity-window-pattern).
**Retention:** Buckets older than 24h + 41s (10s OOO + 31s lateness buffer) are evicted via event-time timer on each write.

**Derived window fields exposed on EnrichedTransaction:**

| Field | Description |
|-------|-------------|
| `vel_count_1m` | Transaction count, last 60 seconds |
| `vel_amount_1m` | Total amount sum, last 60 seconds (decimal 18,4) |
| `vel_count_5m` | Transaction count, last 5 minutes |
| `vel_amount_5m` | Total amount sum, last 5 minutes |
| `vel_count_1h` | Transaction count, last 1 hour |
| `vel_amount_1h` | Total amount sum, last 1 hour |
| `vel_count_24h` | Transaction count, last 24 hours |
| `vel_amount_24h` | Total amount sum, last 24 hours |

**Validation rules:**
- `vel_count_*` ≥ 1 (the current transaction is always included)
- `vel_amount_*` > 0 (assuming valid transaction amounts)
- `vel_count_1m` ≤ `vel_count_5m` ≤ `vel_count_1h` ≤ `vel_count_24h` (monotone nesting)
- On cold-start (no prior history): counts = 1, amounts = current transaction amount — not zero (the current event is always counted in its own window)

**State growth:** 1,440 buckets/account × (4 bytes count + 16 bytes decimal + 8 bytes key) ≈ 40 KB max per account (all 24h buckets populated). In practice far less — typical accounts have a few hundred distinct active minutes/day. At 1M active accounts with 10% bucket fill: 1M × 40KB × 0.10 = ~4 GB/node — at the boundary of SC-005 limit. Configure `state.backend.rocksdb.memory.fixed-per-slot` accordingly and tune parallelism if state exceeds 4 GB.

---

## 4. GeolocationContext (in-process cache)

**Cache type:** Python `functools.lru_cache(maxsize=10_000)` keyed on subnet string (e.g. `"203.0.113.0"`).
**Backing store:** MaxMind GeoLite2-City MMDB (binary file, ~70 MB, mounted at `/opt/flink/geoip/GeoLite2-City.mmdb`).

**Fields exposed on EnrichedTransaction:**

| Field | Type | Description | Nullable |
|-------|------|-------------|----------|
| `geo_country` | string | ISO 3166-1 alpha-2 country code (e.g. `"US"`) | Yes — unresolved subnet → null |
| `geo_city` | string | City name (e.g. `"New York"`) | Yes — city precision not always available |
| `geo_network_class` | string | Network classification: `RESIDENTIAL`, `BUSINESS`, `HOSTING`, `MOBILE`, `UNKNOWN` | Yes |
| `geo_confidence` | float [0.0–1.0] | MaxMind accuracy radius converted to confidence score | Yes (null = lookup failed) |

**Resolution failure handling:**
- Unresolvable subnet (not in GeoLite2 DB): all geo fields → null; processing continues (spec edge case 3)
- Multi-region subnet (subnet spans multiple regions): most-specific match selected (MaxMind's default prefix-length precedence)
- Lookup exception (file missing, I/O error): log error; all geo fields → null; processing continues

---

## 5. DeviceProfile (Flink keyed state)

**State type:** `ValueState<DeviceProfileState>` (serialised as named tuple).
**Key:** `api_key_id` (API channel proxy for `device_id` in v1).

```python
@dataclass(frozen=True)
class DeviceProfileState:
    first_seen_ms: int          # epoch ms of first transaction from this device
    txn_count: int              # lifetime transaction count
    known_fraud: bool           # True if any transaction from this device was labelled fraud
    last_seen_ms: int           # epoch ms of most recent transaction (for TTL timer)
```

**Fields exposed on EnrichedTransaction:**

| Field | Type | Description | Nullable |
|-------|------|-------------|----------|
| `device_first_seen` | long (timestamp-millis) | Event time of first-ever transaction from this device | Yes — null if `api_key_id` absent |
| `device_txn_count` | int | Lifetime transaction count for this device | Yes |
| `device_known_fraud` | boolean | Any prior fraud association | Yes |

**Validation rules:**
- `device_txn_count` ≥ 1 after first transaction
- `device_first_seen` ≤ current `event_time` (monotone)
- If `api_key_id` is null or empty: all device fields → null; processing continues (spec acceptance scenario US4.3)

---

## 6. TransactionDedup (Flink keyed state)

**State type:** `ValueState<Long>` — stores `event_time_ms` of first occurrence.
**Key:** `transaction_id`.
**TTL:** 48h processing time.

**Behaviour:**
- On new transaction: state is null → proceed to enrichment → set state to `event_time_ms`
- On duplicate transaction (state not null): skip enrichment → emit nothing → log duplicate count metric

---

## 7. EnrichedTransaction (output)

Published to Kafka topic `txn.enriched`. Schema: `enriched-txn-v1.avsc` (see `contracts/`).

**Field composition:**

| Field Group | Source |
|-------------|--------|
| All `RawTransaction` fields | Forwarded unchanged from input |
| `vel_count_*`, `vel_amount_*` (8 fields) | VelocityAggregate computation |
| `geo_country`, `geo_city`, `geo_network_class`, `geo_confidence` | GeolocationContext lookup |
| `device_first_seen`, `device_txn_count`, `device_known_fraud` | DeviceProfile state |
| `enrichment_time` | Processor-assigned epoch ms when enrichment completed |
| `enrichment_latency_ms` | Difference between `processing_time` and `enrichment_time` |
| `processor_version` | Processor job version string (injected at deploy time) |

**Kafka topic config:**
```
Topic: txn.enriched
Partitions: same as txn.api (aligned for co-partitioning by account_id)
Replication factor: 3 (production); 1 (local dev)
Cleanup policy: delete
Retention: 7 days
Compression: lz4 (enriched records are ~600–800 bytes; lz4 gives ~30% reduction)
```

---

## 8. DeadLetterRecord (output)

Published to Kafka topic `txn.processing.dlq`. Schema: `processing-dlq-v1.avsc` (see `contracts/`).

**Routing conditions:**

| Error Reason | Trigger |
|---|---|
| `SCHEMA_VALIDATION_ERROR` | Input event fails Avro deserialization or schema validation |
| `LATE_EVENT_BEYOND_ALLOWED_LATENESS` | Event timestamp < (watermark − allowed_lateness) |
| `ENRICHMENT_EXCEPTION` | Unhandled exception during enrichment (should not occur; defensive) |

---

## 9. Window Boundary Definitions

All windows use **event time** (from `RawTransaction.event_time`).

| Window Label | Duration | Boundary Computation |
|---|---|---|
| 1m | 60 seconds | `event_time − 60,000ms` |
| 5m | 300 seconds | `event_time − 300,000ms` |
| 1h | 3,600 seconds | `event_time − 3,600,000ms` |
| 24h | 86,400 seconds | `event_time − 86,400,000ms` |

**Note on late events within allowed-lateness:** When a late event arrives with `event_time = T_late`, window boundaries are computed from `T_late`. The corrected velocity counts are re-emitted on `txn.enriched`. The downstream scoring engine must handle corrected records idempotently (same `transaction_id`, different velocity values — most recent emission wins).

---

## 10. State Storage Estimate

| State Store | Per-Account Size | 1M Accounts |
|---|---|---|
| VelocityAggregate | ~40 KB max (1,440 buckets); ~4 KB typical (10% fill) | ~4 GB max / **~400 MB typical** |
| DeviceProfile | ~64 bytes | ~64 MB |
| TransactionDedup | ~40 bytes | ~40 MB (per 1M unique txn_ids in 48h window) |
| **Total (typical)** | | **~500 MB** |
| **Total (worst case)** | | **~4.1 GB** |

Typical case well within SC-005 (<4 GB/node for 1M active accounts). Worst case (all accounts active across all 1,440 minute buckets simultaneously) approaches the limit — monitor `state.size` metric and add nodes or reduce parallelism per node before reaching 4 GB.
