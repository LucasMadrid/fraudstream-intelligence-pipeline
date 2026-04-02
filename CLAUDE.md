# simple-streaming-pipeline Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-04-01

## Active Technologies
- Kafka topics (`txn.api`, `txn.api.dlq`) — no OLTP database (001-kafka-ingestion-pipeline)
- Python 3.11 (established — aligns with ML/data team tooling) (002-flink-stream-processor)
- Python 3.11 (established — aligns with ML/data team tooling; validated in (002-flink-stream-processor)

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
- 002-flink-stream-processor: Added Python 3.11 (established — aligns with ML/data team tooling; validated in
- 002-flink-stream-processor: Added Python 3.11 (established — aligns with ML/data team tooling)
- 001-kafka-ingestion-pipeline: Added Python 3.11 (aligns with ML/data team tooling — confirmed in research.md)


<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
