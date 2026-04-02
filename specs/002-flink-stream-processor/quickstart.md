# Quickstart: Stateful Stream Processor — Local Development

**Branch**: `002-flink-stream-processor` | **Date**: 2026-03-31

This guide gets you from zero to a running PyFlink enrichment job that reads from `txn.api` and writes enriched records to `txn.enriched` locally in ~10 minutes.

All infra operations are managed via `make`. Run `make <tab>` or check [the Makefile](../../Makefile) to see all targets.

---

## Prerequisites

- Docker + Docker Compose (v2.x)
- Python 3.11 (`pyenv local 3.11` or `python3.11`)
- A MaxMind licence key (free account at maxmind.com — needed for GeoLite2 download)

---

## Make targets reference

| Target | What it does |
|---|---|
| `make bootstrap` | Start all services, wait for health, create topics + register schemas |
| `make infra-up` | `docker compose up -d` + status |
| `make infra-down` | `docker compose down` |
| `make infra-clean` | `docker compose down -v` (removes volumes) |
| `make infra-ps` | Show service status |
| `make infra-logs` | Tail all logs; `SERVICE=broker make infra-logs` for one service |
| `make topics` | Create Kafka topics + register Avro schemas |
| `make update-geoip` | Download GeoLite2-City.mmdb (needs `MAXMIND_LICENCE_KEY`) |
| `make install` | `pip install -e ".[processing]"` |
| `make flink-job` | Run the PyFlink enrichment job |
| `make generate` | Produce synthetic transactions + tail enriched output |
| `make consume` | Tail `txn.enriched` only (no producing) |
| `make test` | Run unit tests with coverage |
| `make test-integration` | Run integration tests |

---

## 1. Install Python dependencies

```bash
make install
```

---

## 2. Download the GeoLite2 database

```bash
export MAXMIND_LICENCE_KEY=your_key_here
make update-geoip
```

> The `infra/geoip/` directory is git-ignored. Re-run this after cloning or after `make infra-clean`.

---

## 3. Bootstrap the local stack

```bash
make bootstrap
```

This single command:
1. Starts all Docker services (`broker`, `schema-registry`, `flink-jobmanager`, `flink-taskmanager`, `minio`, `prometheus`, `grafana`)
2. Waits until Kafka is healthy
3. Creates all Kafka topics (`txn.api`, `txn.api.dlq`, `txn.enriched`, `txn.processing.dlq`)
4. Registers Avro schemas with Schema Registry

Check that everything is healthy:

```bash
make infra-ps
```

**Expected healthy services:**
- `broker` — Kafka/KRaft (port 9092)
- `schema-registry` — Confluent Schema Registry (port 8081)
- `flink-jobmanager` — Flink UI (port 8082)
- `flink-taskmanager`
- `minio` — object store (port 9000; console port 9001)
- `prometheus` (port 9090)
- `grafana` (port 3000)

---

## 4. Run the Flink enrichment job

Open a **dedicated terminal** and leave it running:

```bash
make flink-job
```

The job reads from `txn.api`, applies velocity / geo / device enrichment, and writes to `txn.enriched`. Ctrl+C to stop.

---

## 5. Generate synthetic transactions

In a **second terminal**:

```bash
make generate                          # 10 messages, 500 ms apart
COUNT=50 DELAY=200 make generate       # 50 messages, 200 ms apart
COUNT=0 make generate                  # unlimited loop
```

The generator produces Avro messages to `txn.api` and simultaneously tails `txn.enriched`, printing both sides in real time:

```
Consuming from txn.enriched (Ctrl+C to stop) ...
Producing 10 transactions to txn.api ...
  → produced  txn=3e7a1f2c-...  acct=acc-0003  amount=142.5 USD  ip=8.8.8.1
  ← enriched  txn=3e7a1f2c-...  acct=acc-0003  vel_1m=1  US / Mountain View  device_count=1  latency=312ms
  → produced  txn=9c4d8b1a-...  acct=acc-0007  amount=891.0 EUR  ip=5.9.0.1
  ← enriched  txn=9c4d8b1a-...  acct=acc-0007  vel_1m=1  DE / Nuremberg  device_count=1  latency=289ms
```

- **`vel_1m`** increments as the same account sends multiple transactions within the same minute
- **`device_count`** increments as the same `api_key_id` reappears across messages
- **`geo=null`** for RFC1918 / private IPs (expected)
- **City may be `None`** for CDN/anycast ranges (Cloudflare, AWS CloudFront) — country still resolves

---

## 6. Watch output only

To tail `txn.enriched` without producing (e.g. while the job processes a backlog):

```bash
make consume
```

Or with the raw Kafka CLI:

```bash
docker exec -it broker \
  kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic txn.enriched \
  --from-beginning
```

---

## 7. Observe metrics

- **Flink UI**: [http://localhost:8082](http://localhost:8082) — throughput, backpressure, checkpoint duration
- **Prometheus**: [http://localhost:9090](http://localhost:9090) — query `flink_taskmanager_job_task_operator_*`
- **Grafana**: [http://localhost:3000](http://localhost:3000) (admin/admin)

Key metrics:
- `flink_jobmanager_job_lastCheckpointDuration` — should be < 10,000ms
- `enrichment_latency_p99_ms` — should be < 20ms
- `flink_taskmanager_job_task_operator_currentInputWatermark` — should be advancing

---

## 8. Tear down

```bash
make infra-down     # stop containers, keep volumes
make infra-clean    # stop containers AND remove volumes (full reset)
```

---

## Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `GeoLite2-City.mmdb not found` | GeoIP DB not downloaded | Run Step 2 (`make update-geoip`) |
| `geo=null` for all records | Job started before DB was present, or stale code in memory | Restart `make flink-job` |
| `Schema not registered` | Bootstrap not run | Run `make bootstrap` |
| Job stuck in INITIALIZING | Kafka topic not created | Run `make topics` |
| `Luhn check failed` in generator | Old generator version | Pull latest and re-run |
| `does not appear to be an IPv4 address` | Old generator version | Pull latest and re-run |
| Enrichment latency > 5s locally | JVM warmup on first run | Expected; stabilises after ~10 messages |
| Service not healthy after bootstrap | Docker image pull slow | Run `make infra-ps` and wait, or re-run `make topics` |
