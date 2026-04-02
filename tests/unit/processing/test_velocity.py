"""Unit tests for VelocityProcessFunction window boundary correctness (T021, T022).

Tests use the plain-Python fallback (process_element_pure) that operates
without pyflink, enabling fast unit execution.
"""

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from pipelines.processing.operators.velocity import (
    VelocityProcessFunction,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_op():
    """Return a fresh pure-Python VelocityProcessFunction."""
    return VelocityProcessFunction()


def _ms(minutes: float) -> int:
    """Convert minutes to epoch milliseconds (relative offset)."""
    return int(minutes * 60 * 1000)


# A reference epoch — 2026-01-01 00:00:00 UTC in ms
_T0 = 1_767_225_600_000


# ---------------------------------------------------------------------------
# T021: Window boundary correctness
# ---------------------------------------------------------------------------


class TestColdStart:
    def test_first_transaction_all_counts_are_one(self):
        op = _make_op()
        v = op.process_element_pure(_T0, Decimal("49.99"))

        assert v["vel_count_1m"] == 1
        assert v["vel_count_5m"] == 1
        assert v["vel_count_1h"] == 1
        assert v["vel_count_24h"] == 1

    def test_first_transaction_amounts_equal_input(self):
        op = _make_op()
        amount = Decimal("99.50")
        v = op.process_element_pure(_T0, amount)

        assert v["vel_amount_1m"] == amount
        assert v["vel_amount_5m"] == amount
        assert v["vel_amount_1h"] == amount
        assert v["vel_amount_24h"] == amount


class TestWindowBoundaryCrossing:
    def test_5m_count_excludes_events_older_than_5_minutes(self):
        """Given 3 events older than 5m and 1 current, 5m count = 1 but 1h count = 4."""
        op = _make_op()
        amt = Decimal("10.00")

        # 3 events at t-6m, t-7m, t-8m — outside 5m window
        op.process_element_pure(_T0 - _ms(8), amt)
        op.process_element_pure(_T0 - _ms(7), amt)
        op.process_element_pure(_T0 - _ms(6), amt)

        # Current event at T0
        v = op.process_element_pure(_T0, amt)

        assert v["vel_count_5m"] == 1, "Events at -6/-7/-8m should fall outside 5m window"
        assert v["vel_count_1h"] == 4, "All 4 events within 1h"
        assert v["vel_count_24h"] == 4

    def test_1m_count_excludes_events_older_than_1_minute(self):
        op = _make_op()
        amt = Decimal("5.00")
        op.process_element_pure(_T0 - _ms(2), amt)  # outside 1m window
        v = op.process_element_pure(_T0, amt)

        assert v["vel_count_1m"] == 1
        assert v["vel_count_5m"] == 2  # both within 5m

    def test_24h_boundary(self):
        """Events older than 24h should not appear in 24h count."""
        op = _make_op()
        amt = Decimal("1.00")
        # 1 event at t-25h (outside 24h window)
        op.process_element_pure(_T0 - _ms(25 * 60), amt)
        v = op.process_element_pure(_T0, amt)

        assert v["vel_count_24h"] == 1, "Event at -25h should fall outside 24h window"

    def test_amount_sum_excludes_expired_bucket(self):
        """Amount sum should not include amounts from expired buckets."""
        op = _make_op()
        old_amt = Decimal("1000.00")
        new_amt = Decimal("50.00")

        op.process_element_pure(_T0 - _ms(2), old_amt)  # outside 1m
        v = op.process_element_pure(_T0, new_amt)

        assert v["vel_amount_1m"] == new_amt
        assert v["vel_amount_5m"] == old_amt + new_amt  # both within 5m


class TestMonotoneNesting:
    def test_counts_are_monotone_nested(self):
        """vel_count_1m ≤ vel_count_5m ≤ vel_count_1h ≤ vel_count_24h."""
        op = _make_op()
        amt = Decimal("10.00")
        offsets = [0, -_ms(0.5), -_ms(2), -_ms(10), -_ms(100), -_ms(1440)]
        for off in offsets:
            v = op.process_element_pure(_T0 + off, amt)

        v = op.process_element_pure(_T0, amt)
        assert v["vel_count_1m"] <= v["vel_count_5m"]
        assert v["vel_count_5m"] <= v["vel_count_1h"]
        assert v["vel_count_1h"] <= v["vel_count_24h"]


# ---------------------------------------------------------------------------
# T022: Hypothesis property-based tests
# ---------------------------------------------------------------------------


@st.composite
def velocity_event_strategy(draw):
    """Generate a single (offset_from_T0_ms, amount) pair."""
    offset_ms = draw(st.integers(min_value=-(24 * 60 * 60 * 1000), max_value=0))
    amount_cents = draw(st.integers(min_value=1, max_value=1_000_000))
    amount = Decimal(amount_cents) / 100
    return (offset_ms, amount)


@given(st.lists(velocity_event_strategy(), min_size=1, max_size=200))
@settings(max_examples=200, deadline=5000)
def test_monotone_nesting_property(events):
    """For any valid event sequence, counts must be monotone nested and >= 1."""
    op = _make_op()
    for offset_ms, amount in sorted(events, key=lambda e: e[0]):
        v = op.process_element_pure(_T0 + offset_ms, amount)

    # Final state check: process one more event at T0 to get current velocities
    v = op.process_element_pure(_T0, Decimal("0.01"))

    assert v["vel_count_1m"] >= 1, "Current transaction always included"
    assert v["vel_count_1m"] <= v["vel_count_5m"]
    assert v["vel_count_5m"] <= v["vel_count_1h"]
    assert v["vel_count_1h"] <= v["vel_count_24h"]


@given(st.lists(velocity_event_strategy(), min_size=1, max_size=50))
@settings(max_examples=100, deadline=5000)
def test_amounts_are_positive_property(events):
    """Velocity amount sums must be > 0 for any non-empty sequence."""
    op = _make_op()
    for offset_ms, amount in sorted(events, key=lambda e: e[0]):
        v = op.process_element_pure(_T0 + offset_ms, amount)

    v = op.process_element_pure(_T0, Decimal("0.01"))
    assert v["vel_amount_1m"] > 0
    assert v["vel_amount_24h"] > 0
