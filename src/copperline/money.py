"""Currency-safe decimal money and exchange-rate primitives."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
from typing import Iterable, Iterator

from .errors import CurrencyMismatchError, ValidationError

_CURRENCY_EXPONENTS: dict[str, int] = {
    "BHD": 3,
    "CAD": 2,
    "CHF": 2,
    "CNY": 2,
    "DKK": 2,
    "EUR": 2,
    "GBP": 2,
    "HKD": 2,
    "INR": 2,
    "JPY": 0,
    "KRW": 0,
    "MXN": 2,
    "NOK": 2,
    "NZD": 2,
    "SEK": 2,
    "SGD": 2,
    "USD": 2,
    "ZAR": 2,
}


def currency_exponent(currency: str) -> int:
    code = normalize_currency(currency)
    return _CURRENCY_EXPONENTS.get(code, 2)


def normalize_currency(currency: str) -> str:
    code = str(currency).strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValidationError(f"invalid ISO-style currency code: {currency!r}")
    return code


def decimal(value: Decimal | int | str | float) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, float):
        result = Decimal(str(value))
    else:
        result = Decimal(value)
    if not result.is_finite():
        raise ValidationError("money value must be finite")
    return result


def quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-currency_exponent(currency))


def quantize_amount(
    amount: Decimal | int | str | float,
    currency: str,
    *,
    rounding: str = ROUND_HALF_EVEN,
) -> Decimal:
    return decimal(amount).quantize(quantum(currency), rounding=rounding)


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __init__(
        self,
        amount: Decimal | int | str | float,
        currency: str,
        *,
        rounding: str = ROUND_HALF_EVEN,
    ) -> None:
        code = normalize_currency(currency)
        object.__setattr__(self, "currency", code)
        object.__setattr__(self, "amount", quantize_amount(amount, code, rounding=rounding))

    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(0, currency)

    @classmethod
    def from_minor(cls, units: int, currency: str) -> "Money":
        code = normalize_currency(currency)
        return cls(Decimal(units).scaleb(-currency_exponent(code)), code)

    @property
    def minor_units(self) -> int:
        factor = Decimal(10) ** currency_exponent(self.currency)
        return int(self.amount * factor)

    def _require(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"cannot combine {self.currency} and {other.currency}",
                details={"left": self.currency, "right": other.currency},
            )

    def __add__(self, other: "Money") -> "Money":
        self._require(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._require(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def __abs__(self) -> "Money":
        return Money(abs(self.amount), self.currency)

    def __mul__(self, scalar: Decimal | int | str | float) -> "Money":
        return Money(self.amount * decimal(scalar), self.currency)

    def __rmul__(self, scalar: Decimal | int | str | float) -> "Money":
        return self * scalar

    def __truediv__(self, scalar: Decimal | int | str | float) -> "Money":
        divisor = decimal(scalar)
        if divisor == 0:
            raise ZeroDivisionError
        return Money(self.amount / divisor, self.currency)

    def __bool__(self) -> bool:
        return self.amount != 0

    def __lt__(self, other: "Money") -> bool:
        self._require(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._require(other)
        return self.amount <= other.amount

    def to_dict(self) -> dict[str, str]:
        return {"amount": format(self.amount, "f"), "currency": self.currency}

    def format(self, *, symbol: bool = False) -> str:
        prefix = ""
        if symbol:
            prefix = {
                "USD": "$",
                "EUR": "€",
                "GBP": "£",
                "JPY": "¥",
                "CNY": "¥",
            }.get(self.currency, f"{self.currency} ")
        elif not symbol:
            prefix = f"{self.currency} "
        places = currency_exponent(self.currency)
        return f"{prefix}{self.amount:,.{places}f}"

    def is_zero(self) -> bool:
        return self.amount == 0


@dataclass(frozen=True, slots=True)
class ExchangeRate:
    base: str
    quote: str
    rate: Decimal

    def __init__(self, base: str, quote: str, rate: Decimal | int | str | float) -> None:
        base_code = normalize_currency(base)
        quote_code = normalize_currency(quote)
        value = decimal(rate)
        if value <= 0:
            raise ValidationError("exchange rate must be positive")
        object.__setattr__(self, "base", base_code)
        object.__setattr__(self, "quote", quote_code)
        object.__setattr__(self, "rate", value)

    def convert(self, money: Money) -> Money:
        if money.currency == self.base:
            return Money(money.amount * self.rate, self.quote)
        if money.currency == self.quote:
            return Money(money.amount / self.rate, self.base)
        raise CurrencyMismatchError(
            f"rate {self.base}/{self.quote} cannot convert {money.currency}"
        )

    def inverse(self) -> "ExchangeRate":
        with localcontext() as ctx:
            ctx.prec = 28
            return ExchangeRate(self.quote, self.base, Decimal(1) / self.rate)

    def to_dict(self) -> dict[str, str]:
        return {"base": self.base, "quote": self.quote, "rate": format(self.rate, "f")}


def sum_money(values: Iterable[Money], *, currency: str | None = None) -> Money:
    iterator = iter(values)
    if currency is None:
        try:
            first = next(iterator)
        except StopIteration as exc:
            raise ValidationError("currency is required when summing an empty sequence") from exc
        total = first
    else:
        total = Money.zero(currency)
    for value in iterator:
        total = total + value
    return total


def allocate(
    total: Money,
    weights: Iterable[Decimal | int | str | float],
) -> list[Money]:
    """Allocate money proportionally while preserving exact minor-unit total."""
    weight_values = [decimal(value) for value in weights]
    if any(value < 0 for value in weight_values):
        raise ValidationError("allocation weights may not be negative")
    weight_total = sum(weight_values, Decimal(0))
    if not weight_values:
        return []
    if weight_total == 0:
        raise ValidationError("at least one allocation weight must be positive")

    raw = [total.amount * weight / weight_total for weight in weight_values]
    rounded = [quantize_amount(value, total.currency) for value in raw]
    residual_units = total.minor_units - sum(
        Money(value, total.currency).minor_units for value in rounded
    )

    if residual_units:
        # Largest-remainder style assignment. Ties resolve by input order.
        q = quantum(total.currency)
        remainders = [
            (abs(raw_value - rounded_value), index)
            for index, (raw_value, rounded_value) in enumerate(zip(raw, rounded))
        ]
        remainders.sort(key=lambda item: (-item[0], item[1]))
        direction = 1 if residual_units > 0 else -1
        for offset in range(abs(residual_units)):
            _remainder, index = remainders[offset % len(remainders)]
            rounded[index] += q * direction

    result = [Money(value, total.currency) for value in rounded]
    assert sum(item.minor_units for item in result) == total.minor_units
    return result


def percentage(base: Money, percent: Decimal | int | str | float) -> Money:
    return Money(base.amount * decimal(percent) / Decimal(100), base.currency)


def gross_from_tax_inclusive(
    total: Money,
    rate_percent: Decimal | int | str | float,
) -> tuple[Money, Money]:
    rate = decimal(rate_percent)
    if rate < 0:
        raise ValidationError("tax rate may not be negative")
    if rate == 0:
        return total, Money.zero(total.currency)
    with localcontext() as ctx:
        ctx.prec = 32
        net_raw = total.amount / (Decimal(1) + rate / Decimal(100))
    net = Money(net_raw, total.currency, rounding=ROUND_HALF_UP)
    return net, total - net


def iter_minor_units(start: Money, end: Money) -> Iterator[Money]:
    start._require(end)
    step = 1 if end.minor_units >= start.minor_units else -1
    for units in range(start.minor_units, end.minor_units + step, step):
        yield Money.from_minor(units, start.currency)
