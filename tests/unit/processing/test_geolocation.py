"""Unit tests for GeolocationMapFunction, _do_lookup, _subnet_to_ip."""

from unittest.mock import MagicMock


class TestGeolocationMapFunction:
    """Tests exercise the lookup logic via the open_with_reader fallback."""

    def _make_operator(self, reader_mock):
        from pipelines.processing.operators.geolocation import GeolocationMapFunction

        op = GeolocationMapFunction()
        op.open_with_reader(reader_mock)
        return op

    def _city_record(self, country="US", city="New York"):
        record = MagicMock()
        record.country.iso_code = country
        record.city.name = city
        return record

    def _make_txn(self, subnet="203.0.113.0"):
        txn = MagicMock()
        txn.caller_ip_subnet = subnet
        return txn

    def test_cache_hit_does_not_call_reader_again(self):
        reader = MagicMock()
        reader.city.return_value = self._city_record()
        op = self._make_operator(reader)

        txn = self._make_txn("203.0.113.0")
        result1 = op.map(txn)
        result2 = op.map(txn)

        assert result1[1] == result2[1]
        reader.city.assert_called_once()  # second call served from cache

    def test_unresolvable_subnet_returns_all_null(self):
        reader = MagicMock()
        reader.city.side_effect = Exception("address not found in database")
        op = self._make_operator(reader)

        _, geo = op.map(self._make_txn("192.0.2.0"))

        assert geo["geo_country"] is None
        assert geo["geo_city"] is None
        assert geo["geo_network_class"] is None
        assert geo["geo_confidence"] is None

    def test_io_exception_returns_all_null_without_raising(self):
        reader = MagicMock()
        reader.city.side_effect = OSError("mmdb file not found")
        op = self._make_operator(reader)

        # Must not raise
        _, geo = op.map(self._make_txn("10.0.0.0"))

        assert geo["geo_country"] is None
        assert geo["geo_confidence"] is None

    def test_successful_lookup_populates_all_fields(self):
        reader = MagicMock()
        reader.city.return_value = self._city_record(country="US", city="New York")
        op = self._make_operator(reader)

        _, geo = op.map(self._make_txn("8.8.8.0"))

        assert geo["geo_country"] == "US"
        assert geo["geo_city"] == "New York"
        assert geo["geo_network_class"] == "RESIDENTIAL"
        assert geo["geo_confidence"] == 0.9

    def test_different_subnets_cached_independently(self):
        reader = MagicMock()
        reader.city.side_effect = [
            self._city_record(country="US"),
            self._city_record(country="DE"),
        ]
        op = self._make_operator(reader)

        _, us = op.map(self._make_txn("8.8.8.0"))
        _, de = op.map(self._make_txn("85.214.0.0"))

        assert us["geo_country"] == "US"
        assert de["geo_country"] == "DE"
        assert reader.city.call_count == 2

    def test_map_returns_tuple_of_txn_and_geo(self):
        reader = MagicMock()
        reader.city.return_value = self._city_record()
        op = self._make_operator(reader)

        txn = self._make_txn()
        result = op.map(txn)

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert result[0] is txn

    def test_geo_confidence_is_float_in_valid_range(self):
        reader = MagicMock()
        reader.city.return_value = self._city_record(country="DE", city="Berlin")
        op = self._make_operator(reader)

        _, geo = op.map(self._make_txn("85.214.0.0"))

        if geo["geo_confidence"] is not None:
            assert isinstance(geo["geo_confidence"], float)
            assert 0.0 <= geo["geo_confidence"] <= 1.0

    def test_lru_cache_invalidated_on_reader_reload(self):
        """Reloading the reader (GeoLite2 DB update) must discard the LRU cache.

        When a TaskManager re-opens the GeoLite2 DB (e.g. after make update-geoip
        replaces GeoLite2-City.mmdb on disk), open_with_reader() is called with a
        fresh geoip2.database.Reader.  The old lru_cache must be discarded so that
        subsequent lookups use the new DB data, not stale cached results.

        Closes: specs/002-flink-stream-processor/checklists/cicd.md CHK017
        """
        # Phase 1 — prime the cache with reader1
        reader1 = MagicMock()
        reader1.city.return_value = self._city_record(country="US", city="Old City")
        op = self._make_operator(reader1)

        txn = self._make_txn("203.0.113.0")
        _, geo_before = op.map(txn)
        assert geo_before["geo_country"] == "US"
        assert geo_before["geo_city"] == "Old City"
        assert reader1.city.call_count == 1

        # Phase 2 — reload: open_with_reader with a different reader
        reader2 = MagicMock()
        reader2.city.return_value = self._city_record(country="DE", city="New City")
        op.open_with_reader(reader2)

        # Same subnet — must NOT be served from the old cache
        _, geo_after = op.map(txn)
        assert geo_after["geo_country"] == "DE"
        assert geo_after["geo_city"] == "New City"

        # reader2 was actually called (cache was cleared, not a cache hit)
        assert reader2.city.call_count == 1
        # reader1 was not called again after the reload
        assert reader1.city.call_count == 1


class TestDoLookup:
    """Direct tests for the module-level _do_lookup function."""

    def _city_record(self, country="US", city="New York"):
        record = MagicMock()
        record.country.iso_code = country
        record.city.name = city
        return record

    def test_successful_lookup_returns_all_fields(self):
        from pipelines.processing.operators.geolocation import _do_lookup

        reader = MagicMock()
        reader.city.return_value = self._city_record("US", "New York")

        result = _do_lookup(reader, "8.8.8.1")

        assert result["geo_country"] == "US"
        assert result["geo_city"] == "New York"
        assert result["geo_network_class"] == "RESIDENTIAL"
        assert result["geo_confidence"] == 0.9

    def test_exception_returns_all_none(self):
        from pipelines.processing.operators.geolocation import _do_lookup

        reader = MagicMock()
        reader.city.side_effect = Exception("not found")

        result = _do_lookup(reader, "192.0.2.1")

        assert all(v is None for v in result.values())

    def test_logs_on_lookup_failure(self, caplog):
        import logging

        from pipelines.processing.operators.geolocation import _do_lookup

        reader = MagicMock()
        reader.city.side_effect = RuntimeError("db locked")

        with caplog.at_level(logging.ERROR, logger="pipelines.processing.operators.geolocation"):
            _do_lookup(reader, "10.0.0.1")

        assert any("GeoIP" in r.message for r in caplog.records)


class TestSubnetToIp:
    """Direct tests for _subnet_to_ip."""

    def test_zero_last_octet_becomes_one(self):
        from pipelines.processing.operators.geolocation import _subnet_to_ip

        assert _subnet_to_ip("203.0.113.0") == "203.0.113.1"

    def test_non_zero_last_octet_unchanged(self):
        from pipelines.processing.operators.geolocation import _subnet_to_ip

        assert _subnet_to_ip("203.0.113.5") == "203.0.113.5"

    def test_host_address_unchanged(self):
        from pipelines.processing.operators.geolocation import _subnet_to_ip

        assert _subnet_to_ip("8.8.8.8") == "8.8.8.8"
