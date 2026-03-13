"""Sortable identifiers with recognizable prefixes."""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode(value: int, width: int) -> str:
    chars: list[str] = []
    for _ in range(width):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str, *, now_ms: int | None = None) -> str:
    if not prefix or not prefix.replace("_", "").isalnum() or not prefix[0].isalpha():
        raise ValueError(f"invalid id prefix: {prefix!r}")
    timestamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    entropy = secrets.randbits(80)
    return f"{prefix}_{_encode(timestamp, 10)}{_encode(entropy, 16)}"


def parse_prefix(identifier: str) -> str:
    prefix, sep, _rest = identifier.partition("_")
    if not sep:
        raise ValueError(f"identifier has no prefix: {identifier!r}")
    return prefix
