"""Unit tests for DeviceProcessFunction (T034)."""

from pipelines.processing.operators.device import DeviceProcessFunction

# Reference epoch ms
_T0 = 1_767_225_600_000  # 2026-01-01 00:00:00 UTC


def _make_op():
    return DeviceProcessFunction()


class TestFirstSeenScenario:
    def test_first_transaction_sets_count_to_one(self):
        op = _make_op()
        result = op.process_element_pure("key-abc", _T0)

        assert result["device_txn_count"] == 1

    def test_first_transaction_sets_first_seen(self):
        op = _make_op()
        result = op.process_element_pure("key-abc", _T0)

        assert result["device_first_seen"] == _T0

    def test_first_transaction_known_fraud_is_false(self):
        op = _make_op()
        result = op.process_element_pure("key-abc", _T0)

        assert result["device_known_fraud"] is False


class TestCountAccumulation:
    def test_count_increments_on_each_transaction(self):
        op = _make_op()
        for i in range(1, 21):
            result = op.process_element_pure("key-abc", _T0 + i * 1000)

        assert result["device_txn_count"] == 20

    def test_first_seen_is_immutable_after_first_transaction(self):
        op = _make_op()
        op.process_element_pure("key-abc", _T0)
        op.process_element_pure("key-abc", _T0 + 60_000)
        result = op.process_element_pure("key-abc", _T0 + 120_000)

        assert result["device_first_seen"] == _T0

    def test_21st_transaction_has_correct_count(self):
        op = _make_op()
        for i in range(20):
            op.process_element_pure("key-abc", _T0 + i * 1000)
        result = op.process_element_pure("key-abc", _T0 + 20_000)

        assert result["device_txn_count"] == 21
        assert result["device_first_seen"] == _T0


class TestNullDeviceId:
    def test_null_api_key_id_returns_all_null_fields(self):
        op = _make_op()
        result = op.process_element_pure(None, _T0)

        assert result["device_first_seen"] is None
        assert result["device_txn_count"] is None
        assert result["device_known_fraud"] is None

    def test_empty_string_api_key_id_returns_all_null(self):
        op = _make_op()
        result = op.process_element_pure("", _T0)

        assert result["device_first_seen"] is None
        assert result["device_txn_count"] is None
        assert result["device_known_fraud"] is None

    def test_null_device_id_does_not_raise(self):
        op = _make_op()
        # Must not raise — spec US4.3
        op.process_element_pure(None, _T0)

    def test_null_device_id_does_not_create_state(self):
        """Null api_key_id must not pollute state for a subsequent valid key."""
        op = _make_op()
        op.process_element_pure(None, _T0)
        result = op.process_element_pure("key-abc", _T0 + 1000)

        # State for "key-abc" should start fresh (count=1), not be corrupted
        assert result["device_txn_count"] == 1


class TestFirstSeenMonotoneInvariant:
    def test_first_seen_never_greater_than_current_event_time(self):
        op = _make_op()
        times = [_T0, _T0 + 1000, _T0 + 2000, _T0 + 3000]
        for t in times:
            result = op.process_element_pure("key-abc", t)
            assert result["device_first_seen"] <= t, (
                f"device_first_seen={result['device_first_seen']} > event_time={t}"
            )


class TestIsolation:
    def test_different_devices_tracked_independently(self):
        op = _make_op()
        op.process_element_pure("key-A", _T0)
        op.process_element_pure("key-A", _T0 + 1000)
        result_b = op.process_element_pure("key-B", _T0 + 2000)
        result_a = op.process_element_pure("key-A", _T0 + 3000)

        assert result_a["device_txn_count"] == 3
        assert result_b["device_txn_count"] == 1
        assert result_b["device_first_seen"] == _T0 + 2000
