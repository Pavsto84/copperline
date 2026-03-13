"""Stable domain errors exposed by Copperline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ErrorDetail:
    code: str
    message: str
    details: dict[str, Any] | None = None


class CopperlineError(Exception):
    code = "COPPERLINE_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(CopperlineError):
    code = "VALIDATION_ERROR"


class NotFoundError(CopperlineError):
    code = "NOT_FOUND"

    def __init__(self, entity: str, identifier: str) -> None:
        super().__init__(
            f"{entity} not found: {identifier}",
            details={"entity": entity, "identifier": identifier},
        )


class ConflictError(CopperlineError):
    code = "CONFLICT"


class PeriodLockedError(CopperlineError):
    code = "PERIOD_LOCKED"


class UnbalancedEntryError(CopperlineError):
    code = "UNBALANCED_ENTRY"


class CurrencyMismatchError(CopperlineError):
    code = "CURRENCY_MISMATCH"


class InsufficientStockError(CopperlineError):
    code = "INSUFFICIENT_STOCK"


class AuthenticationError(CopperlineError):
    code = "AUTHENTICATION_ERROR"


class AuthorizationError(CopperlineError):
    code = "AUTHORIZATION_ERROR"


class ReconciliationError(CopperlineError):
    code = "RECONCILIATION_ERROR"


class MigrationError(CopperlineError):
    code = "MIGRATION_ERROR"
