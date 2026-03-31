# simple-streaming-pipeline Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-03-30

## Active Technologies
- Kafka topics (`txn.api`, `txn.api.dlq`) — no OLTP database (001-kafka-ingestion-pipeline)

- Python 3.11 (aligns with ML/data team tooling — confirmed in research.md) + `confluent-kafka-python` (Kafka producer + Schema Registry client), `fastavro` (Avro serialisation), `opentelemetry-sdk`, `prometheus-client` (001-kafka-ingestion-pipeline)

## Project Structure

```text
src/
tests/
```

## Commands

cd src [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] pytest [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] ruff check .

## Code Style

Python 3.11 (aligns with ML/data team tooling — confirmed in research.md): Follow standard conventions

## Recent Changes
- 001-kafka-ingestion-pipeline: Added Python 3.11 (aligns with ML/data team tooling — confirmed in research.md)
- 001-kafka-ingestion-pipeline: Added Python 3.11 (aligns with ML/data team tooling — confirmed in research.md)

- 001-kafka-ingestion-pipeline: Added Python 3.11 (aligns with ML/data team tooling — confirmed in research.md) + `confluent-kafka-python` (Kafka producer + Schema Registry client), `fastavro` (Avro serialisation), `opentelemetry-sdk`, `prometheus-client`

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
