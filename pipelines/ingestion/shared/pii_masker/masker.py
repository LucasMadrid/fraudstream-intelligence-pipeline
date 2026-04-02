"""PII masking: PAN → BIN+last4, IP → subnet."""

import ipaddress
from dataclasses import dataclass

from .config import MaskingConfig
from .validators import extract_pan_parts


@dataclass(frozen=True)
class MaskedPAN:
    """Immutable, safe representation of a masked payment card number."""

    bin6: str
    last4: str
    length: int

    def display(self) -> str:
        """Return a display-safe string: BIN + masked middle + last4."""
        middle_count = self.length - 10
        return f"{self.bin6}{'*' * middle_count}{self.last4}"

    def __repr__(self) -> str:
        return f"MaskedPAN(bin6={self.bin6!r}, last4={self.last4!r})"


class RawPAN:
    """Wrapper around a raw PAN that prevents accidental exposure.

    - __repr__ and __str__ never reveal digits.
    - Pickling is blocked so the raw value cannot leave the process.
    - .mask() is the only way to produce a safe MaskedPAN.
    """

    __slots__ = ("_digits",)

    def __init__(self, pan: str) -> None:
        # Normalise immediately — strips separators but keeps validation lazy
        import re

        self._digits: str = re.sub(r"[\s\-]", "", pan)

    def __repr__(self) -> str:
        return "RawPAN(***)"

    def __str__(self) -> str:
        return "***"

    def __reduce__(self):  # type: ignore[override]
        raise TypeError("RawPAN must not be pickled")

    def mask(self, cfg: MaskingConfig) -> MaskedPAN:
        """Validate and mask the PAN, returning a MaskedPAN.

        Raises InvalidPANError if validation fails and cfg.reject_invalid_pan is True.
        """
        bin6, last4 = extract_pan_parts(self._digits)
        return MaskedPAN(bin6=bin6, last4=last4, length=len(self._digits))


def truncate_ip(ip: str, cfg: MaskingConfig) -> str:
    """Truncate an IP address to its network prefix.

    IPv4 → /{ipv4_prefix} (default /24, returns e.g. "203.0.113.0")
    IPv6 → /{ipv6_prefix} (default /64, returns network address string)

    Raises ValueError for multicast addresses.
    """
    addr = ipaddress.ip_address(ip)

    if addr.is_multicast:
        raise ValueError(f"Multicast addresses must not be stored: {ip!r}")

    if isinstance(addr, ipaddress.IPv4Address):
        prefix = cfg.ipv4_prefix
    else:
        prefix = cfg.ipv6_prefix

    network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    return str(network.network_address)
