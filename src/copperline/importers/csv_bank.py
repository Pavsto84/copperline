"""Configurable CSV bank-statement importer."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from ..errors import ValidationError
from ..money import Money


@dataclass(frozen=True, slots=True)
class CsvBankMapping:
    date_column: str
    description_column: str
    amount_column: str | None = None
    debit_column: str | None = None
    credit_column: str | None = None
    external_id_column: str | None = None
    date_formats: tuple[str, ...] = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y")
    decimal_separator: str = "."
    thousands_separator: str = ","
    invert_sign: bool = False

    def validate(self) -> None:
        if bool(self.amount_column) == bool(self.debit_column or self.credit_column):
            raise ValidationError(
                "CSV mapping must use either amount_column or debit/credit columns"
            )
        if not self.date_column or not self.description_column:
            raise ValidationError("date and description columns are required")


class CsvBankImporter:
    def __init__(self, mapping: CsvBankMapping) -> None:
        mapping.validate()
        self.mapping = mapping

    def parse(
        self,
        text: str,
        *,
        currency: str,
        source_name: str = "statement.csv",
    ) -> list[dict[str, object]]:
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise ValidationError("CSV input has no header row")
        required = {
            self.mapping.date_column,
            self.mapping.description_column,
        }
        if self.mapping.amount_column:
            required.add(self.mapping.amount_column)
        if self.mapping.debit_column:
            required.add(self.mapping.debit_column)
        if self.mapping.credit_column:
            required.add(self.mapping.credit_column)
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValidationError(
                f"CSV input is missing mapped columns: {', '.join(sorted(missing))}"
            )

        result: list[dict[str, object]] = []
        for index, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                posted_date = self._parse_date(row.get(self.mapping.date_column, ""))
                amount = self._parse_amount(row)
            except Exception as exc:
                if isinstance(exc, ValidationError):
                    raise ValidationError(f"CSV row {index}: {exc.message}") from exc
                raise ValidationError(f"CSV row {index}: {exc}") from exc
            description = (row.get(self.mapping.description_column) or "").strip()
            external = (
                (row.get(self.mapping.external_id_column) or "").strip()
                if self.mapping.external_id_column
                else ""
            )
            if not external:
                stable = "\0".join(
                    [
                        source_name,
                        str(index),
                        posted_date,
                        format(amount, "f"),
                        description,
                    ]
                )
                external = "csv_" + hashlib.sha256(stable.encode()).hexdigest()[:24]
            result.append(
                {
                    "posted_date": posted_date,
                    "amount": Money(amount, currency),
                    "description": description,
                    "external_id": external,
                    "metadata": {
                        "source": source_name,
                        "source_row": index,
                        "raw": {key: value for key, value in row.items()},
                    },
                }
            )
        return result

    def _parse_date(self, value: str | None) -> str:
        text = (value or "").strip()
        for fmt in self.mapping.date_formats:
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        raise ValidationError(f"unrecognized date {text!r}")

    def _parse_number(self, value: str | None) -> Decimal:
        text = (value or "").strip()
        if not text:
            return Decimal(0)
        if self.mapping.thousands_separator:
            text = text.replace(self.mapping.thousands_separator, "")
        if self.mapping.decimal_separator != ".":
            text = text.replace(self.mapping.decimal_separator, ".")
        text = text.replace("(", "-").replace(")", "")
        try:
            return Decimal(text)
        except Exception as exc:
            raise ValidationError(f"invalid amount {value!r}") from exc

    def _parse_amount(self, row: dict[str, str]) -> Decimal:
        if self.mapping.amount_column:
            amount = self._parse_number(row.get(self.mapping.amount_column))
        else:
            debit = self._parse_number(
                row.get(self.mapping.debit_column) if self.mapping.debit_column else None
            )
            credit = self._parse_number(
                row.get(self.mapping.credit_column) if self.mapping.credit_column else None
            )
            amount = credit - debit
        return -amount if self.mapping.invert_sign else amount
