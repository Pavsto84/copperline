"""Date/time helpers with accounting-period semantics."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .errors import ValidationError


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_date(value: str | date) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError(f"invalid ISO date: {value}") from exc


def parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"invalid ISO datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise ValidationError("datetime must include an explicit timezone")
    return parsed.astimezone(UTC)


def iso_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValidationError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValidationError("date range end precedes start")

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def overlaps(self, other: "DateRange") -> bool:
        return self.start <= other.end and other.start <= self.end

    def days(self) -> int:
        return (self.end - self.start).days + 1


def month_range(year: int, month: int) -> DateRange:
    return DateRange(date(year, month, 1), date(year, month, monthrange(year, month)[1]))


def fiscal_year_range(year: int, start_month: int = 1) -> DateRange:
    if not 1 <= start_month <= 12:
        raise ValidationError("fiscal start month must be 1..12")
    start_year = year if start_month == 1 else year - 1
    start = date(start_year, start_month, 1)
    return DateRange(start, add_months(start, 12) - timedelta(days=1))
