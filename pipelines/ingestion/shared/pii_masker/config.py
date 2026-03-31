"""Masking configuration dataclass."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MaskingConfig:
    ipv4_prefix: int = 24
    ipv6_prefix: int = 64
    reject_invalid_pan: bool = True
