from __future__ import annotations

import unittest
from decimal import Decimal

from copperline.models import BudgetLine, JournalLine
from copperline.money import ExchangeRate, Money
from tests.support import AppFixture


class BudgetReportsFxTests(unittest.TestCase):
    def setUp(self):
        self.fx = AppFixture()

    def tearDown(self):
        self.fx.close()

    def test_fx_quote_convert_and_inverse(self):
        self.fx.app.fx.set_quote("USD", "EUR", "2026-01-01", "0.8")
        self.assertEqual(
            self.fx.app.fx.convert(Money("100", "USD"), "EUR", as_of="2026-01-02"),
            Money("80", "EUR"),
        )
        self.assertEqual(
            self.fx.app.fx.convert(Money("80", "EUR"), "USD", as_of="2026-01-02"),
            Money("100", "USD"),
        )

    def test_fx_latest_quote_uses_as_of(self):
        self.fx.app.fx.set_quote("USD", "EUR", "2026-01-01", "0.8")
        self.fx.app.fx.set_quote("USD", "EUR", "2026-02-01", "0.9")
        quote = self.fx.app.fx.latest_quote("USD", "EUR", as_of="2026-01-15")
        self.assertEqual(quote.rate, Decimal("0.8"))

    def test_cross_rate(self):
        self.fx.app.fx.set_quote("USD", "EUR", "2026-01-01", "0.8")
        self.fx.app.fx.set_quote("EUR", "GBP", "2026-01-01", "0.75")
        rate = self.fx.app.fx.cross_rate("USD", "GBP", "EUR", as_of="2026-01-01")
        self.assertEqual(rate.rate, Decimal("0.600"))

    def test_weighted_average_rate(self):
        rate = self.fx.app.fx.weighted_average_rate(
            [(Money("100", "USD"), Money("80", "EUR")), (Money("50", "USD"), Money("45", "EUR"))]
        )
        self.assertEqual(rate.base, "USD")
        self.assertEqual(rate.quote, "EUR")
        self.assertEqual(rate.rate.quantize(Decimal("0.0001")), Decimal("0.8333"))

    def test_budget_spread_preserves_annual_total(self):
        budget = self.fx.app.budgets.create("Annual", "USD")
        spread = self.fx.app.budgets.spread_annual(
            budget.id,
            self.fx.account("6000").id,
            "2026-01-01",
            Money("1200", "USD"),
        )
        self.assertEqual(len(spread.lines), 12)
        self.assertEqual(sum(line.amount.amount for line in spread.lines), Decimal("1200.00"))

    def test_budget_weighted_spread(self):
        budget = self.fx.app.budgets.create("Seasonal", "USD")
        spread = self.fx.app.budgets.spread_annual(
            budget.id,
            self.fx.account("6000").id,
            "2026-01-01",
            Money("100", "USD"),
            weights=[2] + [1] * 11,
        )
        self.assertGreater(spread.lines[0].amount.amount, spread.lines[1].amount.amount)
        self.assertEqual(sum(line.amount.minor_units for line in spread.lines), 10000)

    def test_budget_amount_for_dimensions(self):
        budget = self.fx.app.budgets.create(
            "Department",
            "USD",
            [
                BudgetLine(self.fx.account("6000").id, "2026-01-01", Money("100", "USD"), {"dept": "a"}),
                BudgetLine(self.fx.account("6000").id, "2026-01-01", Money("200", "USD"), {"dept": "b"}),
            ],
        )
        amount = self.fx.app.budgets.amount_for(
            budget.id,
            self.fx.account("6000").id,
            "2026-01-01",
            dimensions={"dept": "a"},
        )
        self.assertEqual(amount, Money("100", "USD"))

    def test_profit_and_loss(self):
        self.fx.post_cash_sale("500", "2026-01-10")
        self.fx.app.ledger.create_and_post(
            "2026-01-12",
            "Expense",
            "USD",
            [
                JournalLine(self.fx.account("6000").id, Money("125", "USD"), Money.zero("USD")),
                JournalLine(self.fx.account("1000").id, Money.zero("USD"), Money("125", "USD")),
            ],
        )
        report = self.fx.app.reports.profit_and_loss(start="2026-01-01", end="2026-01-31")
        self.assertEqual(report["revenue_total"]["amount"], "500.00")
        self.assertEqual(report["expense_total"]["amount"], "125.00")
        self.assertEqual(report["net_income"]["amount"], "375.00")

    def test_profit_and_loss_dimensions(self):
        self.fx.app.ledger.create_and_post(
            "2026-01-10",
            "Department sales",
            "USD",
            [
                JournalLine(self.fx.account("1000").id, Money("100", "USD"), Money.zero("USD"), dimensions={"dept": "a"}),
                JournalLine(self.fx.account("4000").id, Money.zero("USD"), Money("100", "USD"), dimensions={"dept": "a"}),
            ],
        )
        report = self.fx.app.reports.profit_and_loss(
            start="2026-01-01", end="2026-01-31", dimensions={"dept": "a"}
        )
        self.assertEqual(report["net_income"]["amount"], "100.00")

    def test_balance_sheet_equation(self):
        self.fx.post_cash_sale("500", "2026-01-10")
        report = self.fx.app.reports.balance_sheet(as_of="2026-01-31")
        self.assertEqual(report["equation_difference"]["amount"], "0.00")

    def test_cash_flow_matches_cash_change(self):
        self.fx.post_cash_sale("100", "2026-01-10")
        report = self.fx.app.reports.cash_flow_indirect(
            start="2026-01-01",
            end="2026-01-31",
            cash_account_ids=[self.fx.account("1000").id],
        )
        self.assertEqual(report["net_cash_change"]["amount"], "100.00")

    def test_receivable_aging(self):
        invoice = self.fx.posted_sale_invoice("100", day="2026-01-01")
        report = self.fx.app.reports.aging(kind="receivable", as_of="2026-03-15")
        self.assertEqual(report["grand_total"]["amount"], "100.00")
        self.assertEqual(report["rows"][0]["party_id"], invoice.party_id)

    def test_budget_variance(self):
        budget = self.fx.app.budgets.create(
            "Monthly",
            "USD",
            [BudgetLine(self.fx.account("4000").id, "2026-01-01", Money("80", "USD"))],
        )
        self.fx.post_cash_sale("100", "2026-01-10")
        report = self.fx.app.reports.budget_variance(
            budget.id, start="2026-01-01", end="2026-01-31"
        )
        row = next(row for row in report["rows"] if row["account"]["code"] == "4000")
        self.assertEqual(row["budget"]["amount"], "80.00")
        self.assertEqual(row["actual"]["amount"], "100.00")
        self.assertEqual(row["variance"]["amount"], "20.00")


if __name__ == "__main__":
    unittest.main()
