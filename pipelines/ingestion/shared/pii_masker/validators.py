"""PAN validation helpers."""

import re


class InvalidPANError(ValueError):
    """Raised when a PAN fails structural or Luhn validation."""


def _luhn_valid(digits: str) -> bool:
    """Return True if the digit string satisfies the Luhn algorithm."""
    total = 0
    reverse = digits[::-1]
    for i, ch in enumerate(reverse):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def extract_pan_parts(pan: str) -> tuple[str, str]:
    """Extract (bin6, last4) from a PAN string after normalisation and Luhn check.

    Accepts 15-digit (Amex) and 16-digit cards. Separators (spaces, hyphens)
    are stripped before processing.

    Raises InvalidPANError if the PAN is structurally invalid or fails Luhn.
    """
    # Strip common separators
    digits = re.sub(r"[\s\-]", "", pan)

    if not digits.isdigit():
        raise InvalidPANError(f"PAN contains non-digit characters: {pan!r}")

    if len(digits) not in (15, 16):
        raise InvalidPANError(f"PAN length {len(digits)} is invalid (expected 15 or 16 digits)")

    if not _luhn_valid(digits):
        raise InvalidPANError("Luhn check failed")

    return digits[:6], digits[-4:]
