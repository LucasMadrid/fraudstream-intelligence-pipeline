"""GeolocationMapFunction — GeoLite2 lookup with LRU cache.

Opens geoip2.database.Reader once in open() per TaskManager worker.
~5-20 µs per lookup with maxminddb-c C extension installed.

See: specs/002-flink-stream-processor/research.md §6
"""

from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)


try:  # pragma: no cover
    from pyflink.datastream.functions import MapFunction, RuntimeContext

    class GeolocationMapFunction(MapFunction):
        """Stateless map: RawTransaction → (RawTransaction, geo_dict)."""

        def __init__(self, geoip_db_path: str = "") -> None:
            self._geoip_db_path = geoip_db_path

        def open(self, _runtime_context: RuntimeContext) -> None:
            import geoip2.database

            try:
                self._reader = geoip2.database.Reader(self._geoip_db_path)
            except (FileNotFoundError, ValueError) as exc:
                logger.warning(
                    "GeoIP DB not found at %s — geo fields will be null: %s",
                    self._geoip_db_path,
                    exc,
                )
                self._reader = None

            @functools.lru_cache(maxsize=10_000)
            def _lookup(subnet_str: str) -> dict:
                return _do_lookup(self._reader, subnet_str)

            self._lookup = _lookup

        def open_with_reader(self, reader) -> None:
            """Inject a mock reader for testing (no filesystem access)."""
            self._reader = reader

            @functools.lru_cache(maxsize=10_000)
            def _lookup(subnet_str: str) -> dict:
                return _do_lookup(self._reader, subnet_str)

            self._lookup = _lookup

        def close(self) -> None:
            if hasattr(self, "_reader") and self._reader is not None:
                self._reader.close()

        def map(self, value):
            # Flink passes (txn, velocity) tuple; unit tests pass plain txn.
            if isinstance(value, tuple):
                txn, velocity = value
                geo = self._lookup(txn.caller_ip_subnet)
                return txn, velocity, geo
            txn = value
            geo = self._lookup(txn.caller_ip_subnet)
            return txn, geo

except ImportError:
    # pyflink not installed — testable plain class

    class GeolocationMapFunction:  # type: ignore[no-redef]  # pragma: no cover
        """Plain-Python stand-in for unit tests."""

        def __init__(self, geoip_db_path: str = "") -> None:
            self._geoip_db_path = geoip_db_path
            self._reader = None

        def open_with_reader(self, reader) -> None:
            """Inject a mock reader for testing."""
            self._reader = reader

            @functools.lru_cache(maxsize=10_000)
            def _lookup(subnet_str: str) -> dict:
                return _do_lookup(self._reader, subnet_str)

            self._lookup = _lookup

        def map(self, txn):
            geo = self._lookup(txn.caller_ip_subnet)
            return txn, geo


def _do_lookup(reader, subnet_str: str) -> dict:
    """GeoLite2 city lookup for a /24 subnet prefix.

    Returns all-None dict on any failure (spec edge case 3).
    """
    try:
        if reader is None:
            raise ValueError("GeoIP reader not initialised")
        ip = _subnet_to_ip(subnet_str)
        record = reader.city(ip)
        return {
            "geo_country": record.country.iso_code,
            "geo_city": record.city.name,
            "geo_network_class": "RESIDENTIAL",
            "geo_confidence": 0.9,
        }
    except Exception as exc:
        logger.error("GeoIP lookup failed for %s: %s", subnet_str, exc)
        return {
            "geo_country": None,
            "geo_city": None,
            "geo_network_class": None,
            "geo_confidence": None,
        }


def _subnet_to_ip(subnet_str: str) -> str:
    """'203.0.113.0/24' or '203.0.113.0' → '203.0.113.1' (representative host for lookup)."""
    host = subnet_str.split("/")[0]
    parts = host.split(".")
    if len(parts) == 4 and parts[-1] == "0":
        parts[-1] = "1"
    return ".".join(parts)
