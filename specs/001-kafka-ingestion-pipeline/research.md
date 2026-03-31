# Research: Kafka Ingestion Pipeline — API Channel

**Branch**: `001-kafka-ingestion-pipeline` | **Date**: 2026-03-30
**Status**: Complete — all NEEDS CLARIFICATION resolved

---

## 1. Producer Configuration (confluent-kafka-python, sub-10ms p99)

**Decision:** `linger.ms=1`, `batch.size=65536`, `acks=all`, `compression.type=none`, `enable.idempotence=True`

**Key config:**
```python
{
    "acks": "all",
    "enable.idempotence": True,
    "linger.ms": 1,
    "queue.buffering.max.ms": 1,   # librdkafka alias — must match linger.ms
    "batch.size": 65536,           # 64 KB; default 16 KB is too small at 10k/s
    "compression.type": "none",    # lz4 only if bandwidth-constrained
    "max.in.flight.requests.per.connection": 5,
    "retries": 2147483647,
    "delivery.timeout.ms": 5000,
}
```

**P99 latency budget (LAN):** batch accumulation 1ms + network RTT 0.5–2ms + broker ISR ack 1–3ms + Python overhead 0.5–1ms = **4–7ms total** — within 10ms budget.

**Why `acks=all`:** Financial events require ISR durability. `acks=1` risks data loss on leader failover — a lost fraud event is a regulatory liability. Cost: ~1–2ms extra round-trip, acceptable.

**Why `compression.type=none`:** At 500-byte payloads, CPU cost of compression exceeds wire savings. Enable lz4 only if network bandwidth becomes the bottleneck.

**Alternatives considered:** `linger.ms=0` (max latency spikes under load), `acks=1` (data loss risk), snappy/zstd compression (CPU overhead > gain at this payload size).

---

## 2. Avro Serialisation Library

**Decision:** `confluent_kafka.schema_registry.avro.AvroSerializer` with `SerializingProducer` (backed by fastavro since v1.7).

**Why not legacy `AvroProducer`:** Deprecated; uses `avro-python3` which is 5–10× slower than fastavro.

**Why not raw fastavro:** Requires manual Schema Registry HTTP management; more code, no benefit.

**Schema caching:** Built-in. First produce → HTTP GET Schema Registry → cache schema ID in-process. All subsequent messages: zero registry round-trips. Pre-warm at startup by calling `produce()` once before entering the hot path.

**Serialisation throughput:** fastavro handles 500k–1M records/sec in CPython 3.11. 10k/s is trivially within budget.

**Alternatives considered:** raw fastavro (more control, more code), Protobuf (see §5).

---

## 3. Avro Schema Design

**Monetary amounts:** `bytes` + `decimal` logical type, `precision=18`, `scale=4`. Never `double`/`float` (IEEE 754 rounding corrupts aggregations). `string` only if cross-platform consumers demand it.

**Enum fields (`channel`):** `string` + application-layer validation (Pydantic `Literal`). Avro `enum` is a breaking-change magnet — new channels require schema evolution and can cause unknown-symbol errors in old consumers.

**Nullability:** Non-nullable by default. Use `["null", T]` union only when absence carries semantic meaning. All evolution-added fields must have defaults (backward-compatibility rule). Never make everything nullable "for safety" — it hides producer bugs.

**Field ordering:** identity → temporal → financial core → parties → context/channel → risk signals → metadata. New fields always appended at end.

**Alternatives considered:** Avro enum (too rigid), `double` for amounts (lossy), nullable-everything (weakens contract).

---

## 4. Schema Registry Configuration

**Subject naming:** `TopicNameStrategy` (subject: `txn.api-value`). One schema type per topic is standard. Promote to `TopicRecordNameStrategy` only if `txn.api` carries multiple event types (not planned).

**Compatibility mode:** `BACKWARD_TRANSITIVE` — new schema can read data written with any previous version. Required for fraud replay and reprocessing scenarios where consumers may lag far behind.

**Breaking changes → new topic:** Adding a required non-nullable field without default is always backward-incompatible. Pattern: freeze `txn.api`, create `txn.api.v2`, dual-write, migrate consumers, decommission old topic. Never bypass Registry compatibility check.

**Startup:** Bounded exponential backoff — 5 retries, delays 1s/2s/4s/8s/16s, then fatal error. Kubernetes: use `initContainer` / `depends_on` for ordering. Never fail silently or proceed schema-less.

**Cache TTL:** Indefinite — schema IDs are immutable once registered. Pre-warm cache at startup before entering produce loop. Producer continues using cached IDs even if Registry goes down mid-run.

---

## 5. Avro vs Protobuf

**Decision:** Avro.

| Criterion | Avro | Protobuf |
|---|---|---|
| Schema Registry | Native | Supported but less tooling |
| Schema evolution ergonomics | Natural (defaults + unions) | More verbose (`optional`, `oneof`) |
| Dynamic deserialization | Yes (no codegen) | Requires `.proto` at decode time |
| Analytics (Spark, Flink, Iceberg) | Native readers everywhere | Plugins/codegen required |
| Payload size (~20 fields) | ~100–300 bytes | ~80–250 bytes (marginal) |
| Python-first team friction | Low | Higher (`.proto` compilation, generated stubs) |

**Protobuf when:** cross-language gRPC reuse, <1ms SLA pipelines, or statically-typed multi-language consumers. None apply here.

---

## 6. Idempotency and Deduplication

**Decision:** Idempotent producer (`enable.idempotence=True`) at the producer level. Application-level `transaction_id` deduplication pushed to downstream stream processor (Flink keyed state).

**Why not transactional producer:** `beginTransaction()`/`commitTransaction()` round-trips add 5–20ms per transaction — incompatible with sub-10ms p99 budget.

**Idempotent producer scope:** Deduplicates retried batches within one producer session (same PID + sequence number). Does NOT deduplicate across producer restarts or by business key (`transaction_id`).

**Downstream dedup pattern:** Flink job on `txn.api` maintains keyed state on `transaction_id`; emits only first occurrence. This is standard in financial streaming systems.

---

## 7. Dead Letter Queue

**Decision:** Separate DLQ producer instance writing to `txn.api.dlq`. Serialisation errors handled inline (sync); broker delivery failures handled in delivery callback (async).

**Inline (serialisation errors):**
```python
try:
    producer.produce(topic="txn.api", value=record, on_delivery=delivery_cb)
except (SerializationError, ValueError) as e:
    dlq_producer.produce(topic="txn.api.dlq",
                         value=json.dumps({"error": str(e), "payload": str(record)}).encode())
    dlq_producer.poll(0)
```

**Async (broker errors):**
```python
def delivery_cb(err, msg):
    if err:
        dlq_producer.produce(topic="txn.api.dlq",
                             value=json.dumps({"error": str(err), "topic": msg.topic()}).encode())
        dlq_producer.poll(0)
```

**Critical:** Never call `flush()` inside a delivery callback — deadlock. Use a separate `dlq_producer` instance to avoid re-entering the main producer's queue.

**DLQ producer config:** `acks=1` (durability less critical), `linger.ms=5`, no idempotence.

---

## 8. PAN Masking

**Decision:** `RawPAN` wrapper with blocked `__repr__`/`__reduce__` at the ingestion boundary; `MaskedPAN` frozen dataclass for the serialised form.

**Algorithm:**
1. Strip separators: `re.sub(r"[\s\-]", "", pan)`
2. Luhn-validate full digit string before masking
3. Extract `bin6 = digits[:6]`, `last4 = digits[-4:]`
4. Discard `RawPAN` immediately; only `MaskedPAN` flows to Kafka serialiser

**Luhn timing:** Validate before masking. Cannot Luhn-check with only BIN+last4. Invalid PANs rejected with `InvalidPANError`; event emitted with `pan_valid: false` sentinel rather than silently masking garbage.

**Leak prevention:** `RawPAN.__repr__` returns `"RawPAN(***)"`. `__reduce__` raises `TypeError` to block pickle serialisation. `MaskedPAN` is a frozen dataclass — its repr only contains `bin6`/`last4`/`length`, all safe to log.

---

## 9. IP Address Masking

**Decision:** IPv4 → `/24` (GDPR pseudonymisation standard). IPv6 → `/64` (preserves fraud signal; document as pseudonymisation not full anonymisation).

```python
import ipaddress

def truncate_ip(ip: str, ipv4_prefix: int = 24, ipv6_prefix: int = 64) -> str:
    addr = ipaddress.ip_address(ip)
    prefix = ipv4_prefix if isinstance(addr, ipaddress.IPv4Address) else ipv6_prefix
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False).network_address)
```

**Edge cases:** Loopback/private IPs truncated normally (valid fraud signals). Multicast source addresses → `ValueError` (data quality error). Unspecified (`0.0.0.0`) → stored as-is.

---

## 10. PII Masking Library Design

**Decision:** Pure-function module + injected frozen `MaskingConfig` dataclass. Not a stateful class.

**Versioning:** Internal PyPI package (`pii-masking==x.y.z`). Embed `masking_lib_version` field in Kafka event schema. Breaking changes (e.g., changing prefix length) increment major version with coordinated producer cut-over.

**Testing:** Hypothesis for invariant properties (middle digits never in output, bin6/last4 always correct, idempotency, garbage-in never panics). pytest parametrize for known-good PAN vectors.

---

## 11. Integration Test: No PII in Topic Bytes

**Decision:** Consume raw `msg.value()` bytes, regex-scan for 15/16-digit sequences, recursively traverse JSON string values, assert IPv4 fields end in `.0`.

**Key:** Scan raw bytes _before_ JSON parsing — catches leakage in Avro schema metadata and binary fields, not just JSON values. Use `(?<!\d)\d{15,16}(?!\d)` with word boundaries to avoid false positives from timestamps.

---

## Resolved Unknowns

| Unknown | Resolution |
|---|---|
| Language/framework | Python 3.11 + confluent-kafka-python ≥ 1.7 |
| Avro vs Protobuf | Avro (lower friction for Python-first team, native analytics support) |
| Avro library | `AvroSerializer` + `SerializingProducer` (not legacy `AvroProducer`) |
| `acks` setting | `"all"` (financial durability requirement) |
| Idempotency scope | Producer idempotence + downstream Flink dedup on `transaction_id` |
| DLQ routing | Inline for serialisation errors; async delivery callback for broker errors |
| Monetary type | `bytes` + `decimal` logical type |
| Channel enum | `string` + application validation |
| IPv6 prefix | `/64` (fraud signal preserved; document as pseudonymisation) |
| Schema compatibility | `BACKWARD_TRANSITIVE` |
| Schema naming | `TopicNameStrategy` |
| Startup behaviour | Bounded exponential backoff (5 retries), then fatal |
