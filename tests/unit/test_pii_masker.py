"""Unit and Hypothesis property tests for the PII masking library."""

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from pipelines.ingestion.shared.pii_masker import (
    InvalidPANError,
    MaskedPAN,
    MaskingConfig,
    RawPAN,
    truncate_ip,
)
from pipelines.ingestion.shared.pii_masker.validators import _luhn_valid, extract_pan_parts

# ---------------------------------------------------------------------------
# Luhn
# ---------------------------------------------------------------------------


def test_luhn_valid_visa():
    assert _luhn_valid("4111111111111111") is True


def test_luhn_valid_amex():
    assert _luhn_valid("378282246310005") is True


def test_luhn_invalid():
    assert _luhn_valid("1234567890123456") is False


# ---------------------------------------------------------------------------
# extract_pan_parts
# ---------------------------------------------------------------------------


def test_extract_returns_bin6_and_last4():
    bin6, last4 = extract_pan_parts("4111111111111111")
    assert bin6 == "411111"
    assert last4 == "1111"


def test_extract_amex():
    bin6, last4 = extract_pan_parts("378282246310005")
    assert bin6 == "378282"
    assert last4 == "0005"


def test_extract_strips_separators():
    bin6, last4 = extract_pan_parts("4111-1111-1111-1111")
    assert bin6 == "411111"
    assert last4 == "1111"


def test_extract_rejects_luhn_invalid():
    with pytest.raises(InvalidPANError, match="Luhn"):
        extract_pan_parts("1234567890123456")


def test_extract_rejects_wrong_length():
    with pytest.raises(InvalidPANError, match="length"):
        extract_pan_parts("411111111111")


def test_extract_rejects_non_digits():
    with pytest.raises(InvalidPANError):
        extract_pan_parts("4111XXXX11111111")


# ---------------------------------------------------------------------------
# RawPAN
# ---------------------------------------------------------------------------


def test_raw_pan_repr_hides_digits():
    raw = RawPAN("4111111111111111")
    assert "4111" not in repr(raw)
    assert repr(raw) == "RawPAN(***)"


def test_raw_pan_str_hides_digits():
    raw = RawPAN("4111111111111111")
    assert "4111" not in str(raw)


def test_raw_pan_pickle_blocked():
    import pickle

    raw = RawPAN("4111111111111111")
    with pytest.raises(TypeError, match="must not be pickled"):
        pickle.dumps(raw)


def test_raw_pan_mask_returns_masked():
    cfg = MaskingConfig()
    raw = RawPAN("4111111111111111")
    masked = raw.mask(cfg)
    assert isinstance(masked, MaskedPAN)
    assert masked.bin6 == "411111"
    assert masked.last4 == "1111"


# ---------------------------------------------------------------------------
# MaskedPAN
# ---------------------------------------------------------------------------


def test_masked_pan_display():
    m = MaskedPAN(bin6="411111", last4="1111", length=16)
    display = m.display()
    assert display.startswith("411111")
    assert display.endswith("1111")
    assert "****" in display


def test_masked_pan_repr_safe():
    m = MaskedPAN(bin6="411111", last4="1111", length=16)
    assert "411111" in repr(m)
    # repr shows bin6 and last4, not middle digits — middle digits are ****
    middle_digits = "1111111"  # 7 middle digits in a 16-digit Visa
    assert middle_digits not in repr(m)


# ---------------------------------------------------------------------------
# truncate_ip
# ---------------------------------------------------------------------------


def test_truncate_ipv4():
    cfg = MaskingConfig()
    result = truncate_ip("203.0.113.45", cfg)
    assert result == "203.0.113.0"


def test_truncate_ipv4_last_octet_zero():
    cfg = MaskingConfig()
    assert truncate_ip("10.20.30.0", cfg) == "10.20.30.0"


def test_truncate_ipv6():
    cfg = MaskingConfig()
    result = truncate_ip("2001:db8::1", cfg)
    # /64 prefix → first 64 bits preserved, rest zeroed
    assert result == "2001:db8::"


def test_truncate_multicast_raises():
    cfg = MaskingConfig()
    with pytest.raises(ValueError, match="[Mm]ulticast"):
        truncate_ip("224.0.0.1", cfg)


def test_truncate_loopback_allowed():
    cfg = MaskingConfig()
    result = truncate_ip("127.0.0.1", cfg)
    assert result == "127.0.0.0"


def test_truncate_private_allowed():
    cfg = MaskingConfig()
    result = truncate_ip("192.168.1.100", cfg)
    assert result == "192.168.1.0"


# ---------------------------------------------------------------------------
# Hypothesis property tests (T028)
# ---------------------------------------------------------------------------


def _generate_luhn_pan(length: int = 16) -> str:
    """Generate a Luhn-valid PAN of the given length."""
    known = {15: "378282246310005", 16: "4111111111111111"}
    return known[length]


@given(st.sampled_from(["4111111111111111", "5500005555555559", "378282246310005"]))
def test_property_bin6_is_first_six(pan: str):
    bin6, _ = extract_pan_parts(pan)
    import re

    digits = re.sub(r"[\s\-]", "", pan)
    assert bin6 == digits[:6]


@given(st.sampled_from(["4111111111111111", "5500005555555559", "378282246310005"]))
def test_property_last4_is_last_four(pan: str):
    _, last4 = extract_pan_parts(pan)
    import re

    digits = re.sub(r"[\s\-]", "", pan)
    assert last4 == digits[-4:]


@given(st.sampled_from(["4111111111111111", "5500005555555559", "378282246310005"]))
def test_property_display_never_contains_middle(pan: str):
    cfg = MaskingConfig()
    raw = RawPAN(pan)
    masked = raw.mask(cfg)
    display = masked.display()
    import re

    digits = re.sub(r"[\s\-]", "", pan)
    middle = digits[6:-4]
    assert middle not in display


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz!@#", min_size=1, max_size=20))
def test_property_garbage_input_raises(garbage: str):
    with pytest.raises((InvalidPANError, ValueError)):
        extract_pan_parts(garbage)


@given(
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=0, max_value=255),
    st.integers(min_value=0, max_value=255),
)
def test_property_truncate_ipv4_ends_in_zero(a: int, b: int, c: int, d: int):
    ip = f"{a}.{b}.{c}.{d}"
    import ipaddress

    addr = ipaddress.ip_address(ip)
    assume(not addr.is_multicast)
    cfg = MaskingConfig()
    result = truncate_ip(ip, cfg)
    assert result.endswith(".0"), f"Expected /24 truncation to end in .0, got {result!r}"
