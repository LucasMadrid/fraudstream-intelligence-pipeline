#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP="${KAFKA_BOOTSTRAP_SERVERS:-localhost:9092}"
REPLICATION_FACTOR="${KAFKA_REPLICATION_FACTOR:-1}"  # 1 for local dev, 3 for production

echo "Creating Kafka topics on ${BOOTSTRAP} ..."

docker exec broker kafka-topics.sh \
  --bootstrap-server "${BOOTSTRAP}" \
  --create --if-not-exists \
  --topic txn.api \
  --partitions 12 \
  --replication-factor "${REPLICATION_FACTOR}" \
  --config retention.ms=220752000000 \
  --config min.insync.replicas=1 \
  --config cleanup.policy=delete

docker exec broker kafka-topics.sh \
  --bootstrap-server "${BOOTSTRAP}" \
  --create --if-not-exists \
  --topic txn.api.dlq \
  --partitions 3 \
  --replication-factor "${REPLICATION_FACTOR}" \
  --config retention.ms=604800000 \
  --config cleanup.policy=delete

echo "Topics created:"
docker exec broker kafka-topics.sh --bootstrap-server "${BOOTSTRAP}" --list | grep "txn\."
