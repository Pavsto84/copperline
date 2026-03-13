"""Canonical JSON utilities."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any

from .errors import ValidationError


def _normalize(value: Any, path: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"non-finite number at {path}")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValidationError(f"non-finite decimal at {path}")
        return format(value, "f")
    if isinstance(value, (list, tuple)):
        return [_normalize(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, dict):
        result = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValidationError(f"JSON object key at {path} is not a string")
            result[key] = _normalize(value[key], f"{path}.{key}")
        return result
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _normalize(value.to_dict(), path)
    raise ValidationError(f"unsupported JSON value at {path}: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def dumps(value: Any, *, pretty: bool = False) -> str:
    normalized = _normalize(value)
    if pretty:
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text: str) -> Any:
    return json.loads(text)
