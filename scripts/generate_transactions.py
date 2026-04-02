"""Synthetic transaction generator + enriched output consumer.

Produces realistic Avro-serialized transactions to txn.api and streams
the enriched output from txn.enriched in real time.

Usage:
    python3.11 scripts/generate_transactions.py [--count N] [--delay MS]
    python3.11 scripts/generate_transactions.py --consume-only   # just watch txn.enriched

Examples:
    python3.11 scripts/generate_transactions.py --count 20 --delay 500
    python3.11 scripts/generate_transactions.py --consume-only
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
import uuid

# Concrete public IPs for realistic geo enrichment results (not subnets)
_PUBLIC_IPS = [
    "8.8.8.1",  # Google DNS (US)
    "1.1.1.1",  # Cloudflare (AU)
    "185.60.216.35",  # Facebook (IE)
    "151.101.1.1",  # Fastly CDN (US)
    "104.16.0.1",  # Cloudflare (US)
    "13.32.0.1",  # AWS CloudFront (US)
    "31.13.64.1",  # Facebook (IE)
    "142.250.80.1",  # Google (US)
    "77.234.44.1",  # (NL)
    "5.9.0.1",  # Hetzner (DE)
]

_MERCHANTS = [
    "merch-amazon",
    "merch-netflix",
    "merch-uber",
    "merch-airbnb",
    "merch-spotify",
    "merch-apple",
    "merch-google",
    "merch-steam",
]

_CHANNELS = ["WEB", "MOBILE", "POS", "API"]
_CURRENCIES = ["USD", "EUR", "GBP", "BRL", "MXN"]
_SCOPES = ["read", "write", "read write"]

# A small pool of accounts so velocity state accumulates across messages
_ACCOUNTS = [f"acc-{i:04d}" for i in range(1, 11)]
_API_KEYS = [f"key-{i}" for i in range(1, 6)]


def _luhn_complete(partial: str) -> str:
    """Append a Luhn check digit to make a valid card number."""
    digits = [int(d) for d in partial]
    # Double every second digit from the right (position counting from check digit slot)
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    check = (10 - (total % 10)) % 10
    return partial + str(check)


def _make_payload() -> dict:
    now_ms = int(time.time() * 1000)
    prefix = random.choice(["4", "5"])
    partial = prefix + "".join([str(random.randint(0, 9)) for _ in range(14)])
    card_number = _luhn_complete(partial)

    return {
        "transaction_id": str(uuid.uuid4()),
        "account_id": random.choice(_ACCOUNTS),
        "merchant_id": random.choice(_MERCHANTS),
        "amount": round(random.uniform(1.0, 5000.0), 2),
        "currency": random.choice(_CURRENCIES),
        "event_time": now_ms,
        "channel": random.choice(_CHANNELS),
        "card_number": card_number,
        "caller_ip": random.choice(_PUBLIC_IPS),
        "api_key_id": random.choice(_API_KEYS),
        "oauth_scope": random.choice(_SCOPES),
        "geo_lat": None,
        "geo_lon": None,
    }


def _consume_enriched(bootstrap_servers: str, stop_event: threading.Event) -> None:
    """Background thread: tail txn.enriched and pretty-print each record."""
    from confluent_kafka import Consumer, KafkaError

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap_servers,
            "group.id": f"generator-monitor-{uuid.uuid4().hex[:8]}",
            "auto.offset.reset": "latest",
            "enable.auto.commit": True,
        }
    )
    consumer.subscribe(["txn.enriched"])

    _GEO_COLOR = "\033[32m"  # green
    _RESET = "\033[0m"

    try:
        while not stop_event.is_set():
            msg = consumer.poll(timeout=0.5)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    print(f"[consumer] error: {msg.error()}")
                continue

            try:
                record = json.loads(msg.value())
            except Exception:
                print(f"[consumer] non-JSON message: {msg.value()!r}")
                continue

            geo = (
                f"{_GEO_COLOR}{record.get('geo_country')} / {record.get('geo_city')}{_RESET}"
                if record.get("geo_country")
                else "geo=null"
            )
            print(
                f"  ← enriched  txn={record.get('transaction_id')}  "
                f"acct={record.get('account_id')}  "
                f"vel_1m={record.get('vel_count_1m')}  "
                f"{geo}  "
                f"device_count={record.get('device_txn_count')}  "
                f"latency={record.get('enrichment_latency_ms')}ms"
            )
    finally:
        consumer.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synthetic transaction generator + enriched consumer"
    )
    parser.add_argument(
        "--count", type=int, default=10, help="Number of transactions to produce (0 = unlimited)"
    )
    parser.add_argument("--delay", type=int, default=500, help="Delay between messages (ms)")
    parser.add_argument("--kafka-brokers", default="localhost:9092")
    parser.add_argument("--schema-registry", default="http://localhost:8081")
    parser.add_argument(
        "--consume-only", action="store_true", help="Only consume txn.enriched, do not produce"
    )
    args = parser.parse_args()

    stop_event = threading.Event()
    consumer_thread = threading.Thread(
        target=_consume_enriched,
        args=(args.kafka_brokers, stop_event),
        daemon=True,
    )
    consumer_thread.start()
    print("Consuming from txn.enriched (Ctrl+C to stop) ...")

    if args.consume_only:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
        return

    from pipelines.ingestion.api.config import ProducerConfig
    from pipelines.ingestion.api.producer import ProducerService

    config = ProducerConfig(
        bootstrap_servers=args.kafka_brokers,
        schema_registry_url=args.schema_registry,
    )
    service = ProducerService(config)
    service.start()

    unlimited = args.count == 0
    print(f"Producing {'unlimited' if unlimited else args.count} transactions to txn.api ...")
    ok = 0
    i = 0
    try:
        while unlimited or i < args.count:
            payload = _make_payload()
            try:
                result = service.publish(payload)
                print(
                    f"  → produced  txn={result.transaction_id}  "
                    f"acct={payload['account_id']}  "
                    f"amount={payload['amount']} {payload['currency']}  "
                    f"ip={payload['caller_ip']}"
                )
                ok += 1
            except Exception as exc:
                print(f"  → ERROR: {exc}")
            i += 1
            if args.delay:
                time.sleep(args.delay / 1000)
    except KeyboardInterrupt:
        pass
    finally:
        service.flush()
        print(
            f"\nProduced {ok}/{i} messages. Waiting for enriched output (Ctrl+C again to exit)..."
        )
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            pass
        stop_event.set()


if __name__ == "__main__":
    main()
