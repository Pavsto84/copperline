"""Tax definitions and line-level inclusive/exclusive calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from .audit import AuditService
from .db import Database
from .errors import ConflictError, NotFoundError, ValidationError
from .models import InvoiceLine, TaxRate
from .money import Money, decimal, gross_from_tax_inclusive


@dataclass(frozen=True, slots=True)
class TaxBreakdown:
    net: Money
    tax: Money
    gross: Money
    tax_code: str | None
    recoverable: Money
    nonrecoverable: Money


class TaxService:
    def __init__(self, db: Database, audit: AuditService | None = None) -> None:
        self.db = db
        self.audit = audit or AuditService(db)

    def create(
        self,
        code: str,
        name: str,
        rate: Decimal | str | int | float,
        account_id: str,
        *,
        inclusive: bool = False,
        recoverable_percent: Decimal | str | int | float = 100,
        actor: str = "system",
    ) -> TaxRate:
        code = code.strip().upper()
        if not code:
            raise ValidationError("tax code may not be empty")
        value = decimal(rate)
        recoverable = decimal(recoverable_percent)
        if value < 0 or value > 1000:
            raise ValidationError("tax rate must be between 0 and 1000 percent")
        if recoverable < 0 or recoverable > 100:
            raise ValidationError("recoverable percent must be 0..100")
        rate_obj = TaxRate(
            code=code,
            name=name.strip() or code,
            rate=value,
            account_id=account_id,
            inclusive=inclusive,
            recoverable_percent=recoverable,
        )
        try:
            self.db.execute(
                """
                INSERT INTO tax_rates(
                    code, name, rate, account_id, inclusive, recoverable_percent, active
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    rate_obj.code,
                    rate_obj.name,
                    format(rate_obj.rate, "f"),
                    rate_obj.account_id,
                    1 if rate_obj.inclusive else 0,
                    format(rate_obj.recoverable_percent, "f"),
                ),
            )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ConflictError(f"tax code already exists: {code}") from exc
            raise
        self.audit.record(actor, "tax.create", code, self.to_dict(rate_obj))
        return rate_obj

    def get(self, code: str) -> TaxRate:
        row = self.db.one("SELECT * FROM tax_rates WHERE code=?", (code.upper(),))
        if row is None:
            raise NotFoundError("tax rate", code)
        return TaxRate(
            code=str(row["code"]),
            name=str(row["name"]),
            rate=Decimal(str(row["rate"])),
            account_id=str(row["account_id"]),
            inclusive=bool(row["inclusive"]),
            recoverable_percent=Decimal(str(row["recoverable_percent"])),
            active=bool(row["active"]),
        )

    def list(self, *, active: bool | None = True) -> list[TaxRate]:
        rows = (
            self.db.all("SELECT * FROM tax_rates ORDER BY code")
            if active is None
            else self.db.all(
                "SELECT * FROM tax_rates WHERE active=? ORDER BY code",
                (1 if active else 0,),
            )
        )
        return [self.get(str(row["code"])) for row in rows]

    def calculate_line(self, line: InvoiceLine) -> TaxBreakdown:
        base = Money(
            line.unit_price.amount
            * line.quantity
            * (Decimal(1) - line.discount_percent / Decimal(100)),
            line.unit_price.currency,
            rounding=ROUND_HALF_UP,
        )
        if line.tax_code is None:
            zero = Money.zero(base.currency)
            return TaxBreakdown(
                net=base,
                tax=zero,
                gross=base,
                tax_code=None,
                recoverable=zero,
                nonrecoverable=zero,
            )
        rate = self.get(line.tax_code)
        if not rate.active:
            raise ValidationError(f"tax code {rate.code} is inactive")
        if rate.inclusive:
            net, tax = gross_from_tax_inclusive(base, rate.rate)
            gross = base
        else:
            tax = Money(base.amount * rate.rate / Decimal(100), base.currency, rounding=ROUND_HALF_UP)
            net = base
            gross = net + tax
        recoverable = Money(
            tax.amount * rate.recoverable_percent / Decimal(100),
            tax.currency,
            rounding=ROUND_HALF_UP,
        )
        return TaxBreakdown(
            net=net,
            tax=tax,
            gross=gross,
            tax_code=rate.code,
            recoverable=recoverable,
            nonrecoverable=tax - recoverable,
        )

    def totals(self, lines: Iterable[InvoiceLine]) -> dict[str, object]:
        line_list = list(lines)
        if not line_list:
            raise ValidationError("at least one invoice line is required")
        currency = line_list[0].unit_price.currency
        net = Money.zero(currency)
        tax = Money.zero(currency)
        gross = Money.zero(currency)
        by_code: dict[str, Money] = {}
        for line in line_list:
            if line.unit_price.currency != currency:
                raise ValidationError("invoice lines use multiple currencies")
            result = self.calculate_line(line)
            net += result.net
            tax += result.tax
            gross += result.gross
            if result.tax_code:
                by_code[result.tax_code] = by_code.get(
                    result.tax_code, Money.zero(currency)
                ) + result.tax
        return {
            "net": net,
            "tax": tax,
            "gross": gross,
            "tax_by_code": by_code,
        }

    @staticmethod
    def to_dict(rate: TaxRate) -> dict[str, object]:
        return {
            "code": rate.code,
            "name": rate.name,
            "rate": format(rate.rate, "f"),
            "account_id": rate.account_id,
            "inclusive": rate.inclusive,
            "recoverable_percent": format(rate.recoverable_percent, "f"),
            "active": rate.active,
        }
