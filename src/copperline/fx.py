"""Foreign-exchange quotes, conversion, and revaluation helpers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Iterable

from .audit import AuditService
from .db import Database
from .errors import NotFoundError, ValidationError
from .models import FxQuote
from .money import ExchangeRate, Money, decimal, normalize_currency
from .timeutil import parse_date


class FxService:
    def __init__(self, db: Database, audit: AuditService | None = None) -> None:
        self.db = db
        self.audit = audit or AuditService(db)

    def set_quote(
        self,
        base: str,
        quote: str,
        day: str | date,
        rate: Decimal | int | str | float,
        *,
        source: str = "manual",
        actor: str = "system",
    ) -> FxQuote:
        base = normalize_currency(base)
        quote = normalize_currency(quote)
        if base == quote:
            raise ValidationError("FX quote currencies must differ")
        value = decimal(rate)
        if value <= 0:
            raise ValidationError("FX quote must be positive")
        quote_obj = FxQuote(
            base=base,
            quote=quote,
            date=parse_date(day),
            rate=value,
            source=source,
        )
        self.db.execute(
            """
            INSERT INTO fx_quotes(base, quote, quote_date, rate, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(base, quote, quote_date)
            DO UPDATE SET rate=excluded.rate, source=excluded.source
            """,
            (
                quote_obj.base,
                quote_obj.quote,
                quote_obj.date.isoformat(),
                format(quote_obj.rate, "f"),
                quote_obj.source,
            ),
        )
        self.audit.record(
            actor,
            "fx.quote",
            f"{base}/{quote}/{quote_obj.date.isoformat()}",
            self.to_dict(quote_obj),
        )
        return quote_obj

    def latest_quote(
        self,
        base: str,
        quote: str,
        *,
        as_of: str | date | None = None,
    ) -> FxQuote:
        base = normalize_currency(base)
        quote = normalize_currency(quote)
        if base == quote:
            return FxQuote(base=base, quote=quote, date=parse_date(as_of or date.today()), rate=1)
        params: list[object] = [base, quote]
        clause = ""
        if as_of is not None:
            clause = "AND quote_date <= ?"
            params.append(parse_date(as_of).isoformat())
        row = self.db.one(
            f"""
            SELECT base, quote, quote_date, rate, source
            FROM fx_quotes
            WHERE base=? AND quote=? {clause}
            ORDER BY quote_date DESC
            LIMIT 1
            """,
            tuple(params),
        )
        if row is not None:
            return FxQuote(
                base=str(row["base"]),
                quote=str(row["quote"]),
                date=parse_date(str(row["quote_date"])),
                rate=Decimal(str(row["rate"])),
                source=str(row["source"]),
            )
        reverse_params: list[object] = [quote, base]
        if as_of is not None:
            reverse_params.append(parse_date(as_of).isoformat())
        reverse = self.db.one(
            f"""
            SELECT base, quote, quote_date, rate, source
            FROM fx_quotes
            WHERE base=? AND quote=? {clause}
            ORDER BY quote_date DESC
            LIMIT 1
            """,
            tuple(reverse_params),
        )
        if reverse is None:
            raise NotFoundError("FX quote", f"{base}/{quote}")
        rate = Decimal(1) / Decimal(str(reverse["rate"]))
        return FxQuote(
            base=base,
            quote=quote,
            date=parse_date(str(reverse["quote_date"])),
            rate=rate,
            source=f"inverse:{reverse['source']}",
        )

    def convert(
        self,
        money: Money,
        target_currency: str,
        *,
        as_of: str | date | None = None,
    ) -> Money:
        target = normalize_currency(target_currency)
        if money.currency == target:
            return money
        quote = self.latest_quote(money.currency, target, as_of=as_of)
        return ExchangeRate(quote.base, quote.quote, quote.rate).convert(money)

    def cross_rate(
        self,
        base: str,
        quote: str,
        via: str,
        *,
        as_of: str | date | None = None,
    ) -> ExchangeRate:
        base = normalize_currency(base)
        quote = normalize_currency(quote)
        via = normalize_currency(via)
        if base == quote:
            return ExchangeRate(base, quote, 1)
        first = self.latest_quote(base, via, as_of=as_of)
        second = self.latest_quote(via, quote, as_of=as_of)
        return ExchangeRate(base, quote, first.rate * second.rate)

    def list_quotes(
        self,
        *,
        base: str | None = None,
        quote: str | None = None,
        start: str | date | None = None,
        end: str | date | None = None,
    ) -> list[FxQuote]:
        clauses = ["1=1"]
        params: list[object] = []
        if base:
            clauses.append("base=?")
            params.append(normalize_currency(base))
        if quote:
            clauses.append("quote=?")
            params.append(normalize_currency(quote))
        if start:
            clauses.append("quote_date>=?")
            params.append(parse_date(start).isoformat())
        if end:
            clauses.append("quote_date<=?")
            params.append(parse_date(end).isoformat())
        return [
            FxQuote(
                base=str(row["base"]),
                quote=str(row["quote"]),
                date=parse_date(str(row["quote_date"])),
                rate=Decimal(str(row["rate"])),
                source=str(row["source"]),
            )
            for row in self.db.all(
                f"""
                SELECT * FROM fx_quotes
                WHERE {' AND '.join(clauses)}
                ORDER BY quote_date, base, quote
                """,
                tuple(params),
            )
        ]

    def weighted_average_rate(
        self,
        amounts: Iterable[tuple[Money, Money]],
    ) -> ExchangeRate:
        pairs = list(amounts)
        if not pairs:
            raise ValidationError("cannot compute weighted FX rate from no amounts")
        base_currency = pairs[0][0].currency
        quote_currency = pairs[0][1].currency
        base_total = Decimal(0)
        quote_total = Decimal(0)
        for base_amount, quote_amount in pairs:
            if base_amount.currency != base_currency or quote_amount.currency != quote_currency:
                raise ValidationError("weighted FX pairs use mixed currencies")
            base_total += base_amount.amount
            quote_total += quote_amount.amount
        if base_total == 0:
            raise ValidationError("weighted FX base total is zero")
        return ExchangeRate(base_currency, quote_currency, quote_total / base_total)

    @staticmethod
    def to_dict(quote: FxQuote) -> dict[str, str]:
        return {
            "base": quote.base,
            "quote": quote.quote,
            "date": quote.date.isoformat(),
            "rate": format(quote.rate, "f"),
            "source": quote.source,
        }
