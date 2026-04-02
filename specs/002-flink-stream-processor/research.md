# Research: Stateful Stream Processor — Transaction Feature Enrichment

**Branch**: `002-flink-stream-processor` | **Date**: 2026-03-31
**Status**: Complete — all unknowns resolved

---

## 1. PyFlink DataStream API: Python–JVM Execution Model

**Decision:** Use PyFlink 1.19 DataStream API with Python `KeyedProcessFunction`, with Arrow batching mandatory for the latency budget.

**Execution model:**
PyFlink DataStream user functions run in a **Python subprocess per-TaskManager slot**, communicating with the JVM via Unix-domain socket (Beam Fn API / gRPC). The JVM manages all state storage (RocksDB) and checkpointing; business logic runs in Python. Every `state.value()` / `state.update()` call crosses the JVM–Python boundary.

**Per-record overhead (realistic measurements):**

| Mode | Overhead |
|------|----------|
| Pickle serialisation, bundle size=1 | 0.5–2 ms per record |
| Arrow batching, bundle size=1000 | ~10–50 µs amortized |

**Required config for <20ms budget:**
```python
# flink-conf.yaml — mandatory for latency budget compliance
python.fn-execution.bundle.size: 1000
python.fn-execution.arrow.batch.size: 1000
python.fn-execution.bundle.time: 15   # ms — max wait before flushing a bundle
```

With bundle size=1000 and a 15ms bundle timeout, individual record latency is bounded by the bundle timeout (≤15ms), well within the 20ms Kafka→enrichment budget.

**Key implication:** State reads/writes cross the Python–JVM bridge per call. Use `MapState<minute_bucket, (count, sum)>` (O(1) reads/writes per window bucket) rather than `ListState<(ts, amount)>` (O(N) scan on every event).

**Alternative considered — Java Flink operators:** Zero Python–JVM bridge overhead. Rejected because: abandons Python 3.11 toolchain; Java skillset not available on the team; Arrow batching brings overhead within budget at 5k TPS.

**Alternative considered — Bytewax:** Python-native, avoids JVM entirely. Rejected because: no production-grade exactly-once guarantee in v0.x; incremental checkpoint support absent.

---

## 2. Exactly-Once End-to-End Configuration

**Decision:** Flink checkpoint mode `EXACTLY_ONCE` + Kafka source with `OffsetsInitializer.committedOffsets()` + Kafka sink with `DeliveryGuarantee.EXACTLY_ONCE`.

**Required configuration:**

```python
# CheckpointConfig
env.enable_checkpointing(60_000)                  # 60s interval
env.get_checkpoint_config().set_checkpointing_mode(CheckpointingMode.EXACTLY_ONCE)
env.get_checkpoint_config().set_min_pause_between_checkpoints(10_000)
env.get_checkpoint_config().set_checkpoint_timeout(30_000)   # must be < transaction.timeout.ms

# Kafka source — isolation.level=read_committed is MANDATORY on the source too
# (to skip uncommitted messages from any other transactional producer upstream)
KafkaSource.builder() \
    .set_starting_offsets(OffsetsInitializer.committed_offsets()) \
    .set_property("isolation.level", "read_committed") \
    .set_property("enable.auto.commit", "false") \
    .build()

# Kafka sink (FlinkKafkaProducer / KafkaSink)
KafkaSink.builder() \
    .set_delivery_guarantee(DeliveryGuarantee.EXACTLY_ONCE) \
    .set_transactional_id_prefix("flink-enrichment-") \
    .set_kafka_producer_config({
        "transaction.timeout.ms": "900000",   # must be > checkpoint interval + timeout; broker default is 900000ms
        "enable.idempotence": "true",
    }) \
    .build()
```

**Downstream consumer requirement:** ALL consumers of `txn.enriched` MUST set `isolation.level=read_committed` to see only committed (non-aborted) records. Without this, consumers receive records from in-flight or aborted transactions — violating exactly-once semantics at the consumer boundary.

**Known pitfalls:**
1. **Zombie transactions:** If a Flink TaskManager dies mid-checkpoint, its open Kafka transaction is left open until `transaction.timeout.ms` expires. Set this conservatively (5 minutes) while keeping checkpoint interval short (60s). The Kafka broker default `transaction.max.timeout.ms` is 15 minutes — sufficient.
2. **transactional_id prefix scope:** Each parallel subtask gets a suffix. If parallelism increases, old `transactional_id`s may still be live; Kafka rejects a new producer claiming the same ID until the old one's timeout expires. Avoid increasing parallelism without draining the old deployment first, or use distinct ID prefixes on redeployment.
3. **Flink sink pre-commit / barrier alignment:** The 2PC commit is tied to checkpoint barrier completion. If checkpoints fail repeatedly, the Kafka transaction is neither committed nor aborted until the timeout — this can block `read_committed` consumers for up to `transaction.timeout.ms`.

---

## 3. Velocity Window Pattern

**Decision:** `KeyedProcessFunction` with `MapState<Long, Tuple2<Integer, Decimal>>` — keyed by truncated minute bucket (`event_time_ms // 60_000`), value = `(count, amount_sum)` for that minute.

**Why `MapState<bucket>` over `ListState<(ts, amount)>`:**
`ListState` requires an O(N) scan on every new event to filter by window boundary (N = events in 24h window). At typical account velocity (≤100 txns/day), N is small but still requires iterating all entries on every JVM–Python bridge call.

`MapState<minute_bucket>` reduces each window computation to O(1440) integer range lookups (24h / 1min = 1440 buckets max). Each event only increments one bucket — O(1) write. Eviction deletes buckets older than 24h + watermark_lag — O(stale buckets), not O(all events).

**Pattern:**
```python
class VelocityProcessFunction(KeyedProcessFunction):
    def open(self, ctx):
        descriptor = MapStateDescriptor(
            "vel_buckets", Types.LONG(), Types.TUPLE([Types.INT(), Types.BIG_DEC()])
        )
        self._buckets = self.get_runtime_context().get_map_state(descriptor)

    def process_element(self, txn, ctx):
        event_time_ms = ctx.timestamp()
        bucket_key = event_time_ms // 60_000        # truncate to minute

        existing = self._buckets.get(bucket_key) or (0, Decimal("0"))
        self._buckets.put(bucket_key, (existing[0] + 1, existing[1] + txn.amount))

        # Compute window aggregates from bucket scan
        windows = [(1, "1m"), (5, "5m"), (60, "1h"), (1440, "24h")]
        velocity = {}
        for mins, label in windows:
            cutoff_bucket = bucket_key - mins
            count, total = 0, Decimal("0")
            for bk, (c, a) in self._buckets.items():
                if bk >= cutoff_bucket:
                    count += c
                    total += a
            velocity[f"vel_count_{label}"] = count
            velocity[f"vel_amount_{label}"] = total

        # Register timer for bucket eviction (buckets > 24h + lateness window)
        ctx.timer_service().register_event_time_timer(
            event_time_ms + (24 * 3600 + 41) * 1000   # 24h + 10s OOO + 31s lateness buffer
        )

        yield (txn, velocity)

    def on_timer(self, timestamp, ctx):
        cutoff_bucket = (timestamp - (24 * 3600 + 41) * 1000) // 60_000
        for bk in list(self._buckets.keys()):
            if bk < cutoff_bucket:
                self._buckets.remove(bk)
```

**Why not `SlidingEventTimeWindows`:**
A 24h window with 1-minute slide creates 1,440 panes. Each event is copied into all 1,440 panes — memory and CPU cost is O(window_size / slide_step). For 4 different window sizes simultaneously, this is impractical.

**Late event handling:** When a late event arrives (within allowed-lateness window), `process_element` fires on the late record. The function increments the correct minute bucket and recomputes velocities — producing a corrected enriched record automatically.

---

## 4. State TTL: Event Time vs Processing Time

**Decision:** `StateTtlConfig` uses **processing time** only (Flink limitation). Event-time TTL is not supported natively.

**Flink limitation:** `StateTtlConfig` always measures TTL against wall-clock time (processing time), regardless of `TimeCharacteristic`. There is no event-time TTL in Flink's state API.

**Workaround for 7-day idle TTL:**
Register an event-time timer in `VelocityProcessFunction` on each event for `event_time + 7_days`. When the timer fires:
```python
def on_timer(self, timestamp, ctx):
    # No recent events within 7 days from this timestamp
    self._events.clear()
```

This provides event-time-driven eviction: state is cleared when 7 days of event time have passed since the last event. Combined with `StateTtlConfig` as a fallback (processing-time 14-day cleanup), this bounds state growth.

**Configuration applied:**
- `StateTtlConfig` TTL: 14 days (processing time — safety fallback)
- Event-time timer: 7 days after each event's timestamp (primary eviction mechanism)

---

## 5. RocksDB Incremental Checkpoints

**Decision:** `EmbeddedRocksDBStateBackend` with `incremental=True`.

```python
env.set_state_backend(EmbeddedRocksDBStateBackend(incremental=True))
env.get_checkpoint_config().set_externalized_checkpoint_cleanup(
    ExternalizedCheckpointCleanup.RETAIN_ON_CANCELLATION
)
```

**Size reduction:** For a 4 GB state with ~10–20% change rate per checkpoint interval (60s), incremental checkpoints transfer ~200–800 MB vs 3–5 GB full snapshot — **5–15× reduction** in data transferred. Steady-state converges to ~5–10% of total state per interval. First checkpoint is always full.

**Key configs in `flink-conf.yaml`:**
```yaml
state.backend: rocksdb
state.backend.incremental: true
state.checkpoints.num-retained: 3          # must be >=3 to maintain incremental chain
state.backend.rocksdb.memory.managed: true
state.backend.rocksdb.memory.fixed-per-slot: 512mb
state.backend.rocksdb.checkpoint.transfer.thread.num: 4
state.backend.rocksdb.timer-service.factory: ROCKSDB  # event-time timers in RocksDB, not heap
state.checkpoints.dir: s3://flink-checkpoints/002-stream-processor
taskmanager.memory.process.size: 4g
```

**Pitfall — checkpoint chain integrity:** Incremental checkpoints reference shared SST files across checkpoints. Never delete intermediate checkpoints without retaining the full history chain. Use `state.checkpoints.num-retained >= 3`. Deleting a checkpoint in the middle of the chain will break recovery.

**Recovery time:** With incremental checkpoints on S3/MinIO, recovery downloads only changed SST files. At 200–800 MB changed state and 100 MB/s network, recovery is 2–8s — well within SC-003's 60s budget.

---

## 6. GeoIP2 + GeoLite2 Embedded in PyFlink

**Decision:** `geoip2.database.Reader` opened once per TaskManager worker via `open()` method; LRU cache on `/24` subnet prefix (maxsize=10,000).

```python
class GeolocationMapFunction(MapFunction):
    def open(self, ctx):
        import geoip2.database
        from functools import lru_cache
        self._reader = geoip2.database.Reader("/opt/flink/geoip/GeoLite2-City.mmdb")

        @lru_cache(maxsize=10_000)
        def _lookup(subnet_str: str):
            try:
                ip = subnet_str.rstrip(".0") + ".1"  # represent /24 subnet
                record = self._reader.city(ip)
                return {
                    "geo_country": record.country.iso_code,
                    "geo_city": record.city.name,
                    "geo_network_class": "RESIDENTIAL",  # simplified
                    "geo_confidence": 0.9,
                }
            except Exception:
                return {"geo_country": None, "geo_city": None,
                        "geo_network_class": None, "geo_confidence": 0.0}

        self._lookup = _lookup

    def map(self, txn):
        geo = self._lookup(txn.caller_ip_subnet)
        return txn, geo
```

**Lookup latency:**
- Pure Python `maxminddb` reader (default): ~0.1–0.5 ms per lookup
- With `maxminddb-c` C extension installed: ~5–20 µs per lookup

Install `maxminddb-c` in the TaskManager environment to meet latency budget. The GeoLite2-City.mmdb is memory-mapped — after the first few lookups it is hot in the OS page cache; subsequent calls are pure in-memory radix traversals.

**Bundling:** The `GeoLite2-City.mmdb` file (~60 MB) is mounted via Docker volume in local dev (`infra/geoip/GeoLite2-City.mmdb`). In Kubernetes, it's provided via an init container or volume mount. Not committed to the repo (binary file; updated weekly). For PyFlink cluster deployment, distribute via `env.add_python_file("GeoLite2-City.mmdb")` and open with `Reader("GeoLite2-City.mmdb")` in `open()`.

**MaxMind licence:** GeoLite2 requires a free MaxMind account + licence key for download. Download automated via `make update-geoip` targeting MaxMind's GeoLite2 permalink.

---

## 7. Watermark Strategy + Allowed Lateness

**Decision:**
```python
WatermarkStrategy \
    .for_bounded_out_of_orderness(Duration.of_seconds(10)) \
    .with_timestamp_assigner(
        lambda event, ts: event.event_time  # epoch ms from txn_api_v1.event_time
    )
```

- **Max out-of-orderness:** 10 seconds (FR-013 default, configurable via `ProcessorConfig.watermark_out_of_orderness_seconds`)
- **Allowed lateness:** 30 seconds (FR-014 default, configurable via `ProcessorConfig.allowed_lateness_seconds`)
- **Events beyond allowed lateness:** Forwarded to `txn.processing.dlq` with `error_type: LATE_EVENT_BEYOND_ALLOWED_LATENESS`

**Watermark stall detection:** A Flink metric `currentWatermark` is exposed per subtask. A Prometheus alert fires if the watermark hasn't advanced in 60 seconds (configurable), indicating a stalled partition (edge case from spec).

---

## 8. Deduplication (transaction_id)

**Decision:** Keyed state dedup on `transaction_id` using `ValueState<Long>` (first-seen event_time). If a record with the same `transaction_id` has been seen before, skip enrichment and do not emit output.

This handles the Kafka at-least-once delivery guarantee at the source boundary. Combined with Flink's exactly-once checkpoint semantics, this ensures each `transaction_id` is reflected in velocity aggregates exactly once.

**State entry TTL:** 48 hours (processing time) — covers the Kafka redelivery window. Uses `StateTtlConfig` with processing time (sufficient for dedup; no need for event-time accuracy here).

---

## Resolved Unknowns

| Unknown | Resolution |
|---|---|
| Runtime: PyFlink vs Java Flink | PyFlink 1.19 DataStream API — Arrow batching (bundle.size=1000, bundle.time=15ms) brings amortized overhead to ~10–50 µs |
| State backend | EmbeddedRocksDB + incremental checkpoints — disk-spilling, 5–15× checkpoint size reduction; retain ≥3 checkpoints for chain integrity |
| Exactly-once coordination | Checkpoint mode EXACTLY_ONCE + KafkaSink `DeliveryGuarantee.EXACTLY_ONCE` + `isolation.level=read_committed` on BOTH source AND downstream consumers; `transaction.timeout.ms=900000` |
| Velocity window pattern | `KeyedProcessFunction` + `MapState<minute_bucket, (count, sum)>` — O(1) write, O(1440) bucket scan for 24h vs O(1440 pane copies) for sliding windows |
| State TTL eviction | Event-time timer at event_time+7d (primary) + StateTtlConfig 14d processing time (fallback); `StateTtlConfig` is processing-time only (Flink limitation) |
| Checkpoint storage | MinIO S3-compatible (local dev) + `s3://flink-checkpoints/` path |
| GeoIP lookup | geoip2 `Reader` per TaskManager + `maxminddb-c` C extension (5–20 µs/lookup) + LRU cache on /24 subnet |
| Watermark strategy | `forBoundedOutOfOrderness(10s)` + 30s allowed lateness + DLQ for beyond-window late events |
| Deduplication | `ValueState<Long>` keyed on `transaction_id` with 48h TTL |
