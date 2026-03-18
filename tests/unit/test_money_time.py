from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

from copperline.errors import CurrencyMismatchError, ValidationError
from copperline.money import (
    ExchangeRate,
    Money,
    allocate,
    currency_exponent,
    gross_from_tax_inclusive,
    iter_minor_units,
    percentage,
    sum_money,
)
from copperline.timeutil import DateRange, add_months, fiscal_year_range, parse_datetime


class MoneyTests(unittest.TestCase):
    def test_money_quantizes_currency(self):
        self.assertEqual(Money("1.005", "USD").amount, Decimal("1.00"))
        self.assertEqual(Money("1.006", "USD").amount, Decimal("1.01"))

    def test_jpy_uses_zero_decimal_places(self):
        self.assertEqual(currency_exponent("JPY"), 0)
        self.assertEqual(Money("10.6", "JPY").amount, Decimal("11"))

    def test_minor_units_roundtrip(self):
        value = Money.from_minor(12345, "USD")
        self.assertEqual(value.amount, Decimal("123.45"))
        self.assertEqual(value.minor_units, 12345)

    def test_addition_requires_same_currency(self):
        with self.assertRaises(CurrencyMismatchError):
            _ = Money("1", "USD") + Money("1", "EUR")

    def test_subtraction_and_negation(self):
        value = Money("10", "USD") - Money("3.25", "USD")
        self.assertEqual(value, Money("6.75", "USD"))
        self.assertEqual(-value, Money("-6.75", "USD"))

    def test_scalar_multiplication(self):
        self.assertEqual(Money("2.50", "USD") * Decimal("1.5"), Money("3.75", "USD"))

    def test_sum_money(self):
        values = [Money("1", "USD"), Money("2.50", "USD"), Money("-0.25", "USD")]
        self.assertEqual(sum_money(values), Money("3.25", "USD"))

    def test_empty_sum_requires_currency(self):
        with self.assertRaises(ValidationError):
            sum_money([])
        self.assertEqual(sum_money([], currency="USD"), Money.zero("USD"))

    def test_allocation_preserves_total_minor_units(self):
        result = allocate(Money("10.00", "USD"), [1, 1, 1])
        self.assertEqual(sum(item.minor_units for item in result), 1000)
        self.assertEqual(result, [Money("3.34", "USD"), Money("3.33", "USD"), Money("3.33", "USD")])

    def test_allocation_handles_negative_total(self):
        result = allocate(Money("-1.00", "USD"), [1, 2])
        self.assertEqual(sum(item.minor_units for item in result), -100)
        self.assertEqual(result, [Money("-0.33", "USD"), Money("-0.67", "USD")])

    def test_allocation_rejects_zero_weights(self):
        with self.assertRaises(ValidationError):
            allocate(Money("1", "USD"), [0, 0])

    def test_percentage(self):
        self.assertEqual(percentage(Money("250", "USD"), "7.5"), Money("18.75", "USD"))

    def test_tax_inclusive_split(self):
        net, tax = gross_from_tax_inclusive(Money("110.00", "USD"), 10)
        self.assertEqual(net, Money("100.00", "USD"))
        self.assertEqual(tax, Money("10.00", "USD"))

    def test_exchange_rate_converts_and_inverts(self):
        rate = ExchangeRate("USD", "EUR", "0.8")
        self.assertEqual(rate.convert(Money("10", "USD")), Money("8", "EUR"))
        self.assertEqual(rate.inverse().convert(Money("8", "EUR")), Money("10", "USD"))

    def test_exchange_rate_rejects_unrelated_currency(self):
        rate = ExchangeRate("USD", "EUR", "0.8")
        with self.assertRaises(CurrencyMismatchError):
            rate.convert(Money("1", "GBP"))

    def test_iter_minor_units_inclusive(self):
        values = list(iter_minor_units(Money("0.01", "USD"), Money("0.03", "USD")))
        self.assertEqual(values, [Money("0.01", "USD"), Money("0.02", "USD"), Money("0.03", "USD")])

    def test_money_format(self):
        self.assertEqual(Money("1234.5", "USD").format(), "USD 1,234.50")
        self.assertEqual(Money("1234.5", "USD").format(symbol=True), "$1,234.50")


class TimeTests(unittest.TestCase):
    def test_parse_datetime_requires_timezone(self):
        with self.assertRaises(ValidationError):
            parse_datetime("2026-01-01T10:00:00")

    def test_parse_datetime_normalizes_to_utc(self):
        value = parse_datetime("2026-01-01T10:00:00+02:00")
        self.assertEqual(value, datetime(2026, 1, 1, 8, tzinfo=UTC))

    def test_add_months_clamps_end_of_month(self):
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(add_months(date(2025, 1, 31), 1), date(2025, 2, 28))

    def test_date_range_is_inclusive(self):
        value = DateRange(date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(value.days(), 31)
        self.assertTrue(value.contains(date(2026, 1, 31)))

    def test_date_range_overlap(self):
        a = DateRange(date(2026, 1, 1), date(2026, 1, 10))
        b = DateRange(date(2026, 1, 10), date(2026, 1, 20))
        self.assertTrue(a.overlaps(b))

    def test_fiscal_year_non_january(self):
        value = fiscal_year_range(2026, 7)
        self.assertEqual(value.start, date(2025, 7, 1))
        self.assertEqual(value.end, date(2026, 6, 30))


if __name__ == "__main__":
    unittest.main()
