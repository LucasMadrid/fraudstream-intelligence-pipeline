#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP="${KAFKA_BOOTSTRAP_SERVERS:-localhost:9092}"
REPLICATION_FACTOR="${KAFKA_REPLICATION_FACTOR:-1}"  # 1 for local dev, 3 for production

echo "Creating Kafka topics on ${BOOTSTRAP} ..."

docker exec broker kafka-topics \
  --bootstrap-server "${BOOTSTRAP}" \
  --create --if-not-exists \
  --topic txn.api \
  --partitions 12 \
  --replication-factor "${REPLICATION_FACTOR}" \
  --config retention.ms=220752000000 \
  --config min.insync.replicas=1 \
  --config cleanup.policy=delete

docker exec broker kafka-topics \
  --bootstrap-server "${BOOTSTRAP}" \
  --create --if-not-exists \
  --topic txn.api.dlq \
  --partitions 3 \
  --replication-factor "${REPLICATION_FACTOR}" \
  --config retention.ms=604800000 \
  --config cleanup.policy=delete

docker exec broker kafka-topics \
  --bootstrap-server "${BOOTSTRAP}" \
  --create --if-not-exists \
  --topic txn.enriched \
  --partitions 3 \
  --replication-factor "${REPLICATION_FACTOR}" \
  --config cleanup.policy=delete \
  --config retention.ms=604800000 \
  --config compression.type=lz4

docker exec broker kafka-topics \
  --bootstrap-server "${BOOTSTRAP}" \
  --create --if-not-exists \
  --topic txn.processing.dlq \
  --partitions 3 \
  --replication-factor "${REPLICATION_FACTOR}" \
  --config cleanup.policy=delete \
  --config retention.ms=604800000

echo "Topics created:"
docker exec broker kafka-topics --bootstrap-server "${BOOTSTRAP}" --list | grep "txn\."

# ── Schema Registry registration ──────────────────────────────────────────
SCHEMA_REGISTRY="${SCHEMA_REGISTRY_URL:-http://localhost:8081}"

echo "Registering Avro schemas with Schema Registry at ${SCHEMA_REGISTRY} ..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

_register_schema() {
  local subject="$1"
  local schema_file="$2"
  local schema_json
  schema_json=$(python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' < "${schema_file}")
  curl -sS -o /dev/null -w "%{http_code}" \
    -X POST "${SCHEMA_REGISTRY}/subjects/${subject}/versions" \
    -H "Content-Type: application/vnd.schemaregistry.v1+json" \
    -d "{\"schema\": ${schema_json}}"
  echo " — registered ${subject}"
}

_register_schema \
  "txn.enriched-value" \
  "${REPO_ROOT}/specs/002-flink-stream-processor/contracts/enriched-txn-v1.avsc"

_register_schema \
  "txn.processing.dlq-value" \
  "${REPO_ROOT}/specs/002-flink-stream-processor/contracts/processing-dlq-v1.avsc"

echo "Schema registration complete."
