from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from copperline import Copperline
from copperline.models import InvoiceLine, PaymentAllocation
from copperline.money import Money


class BusinessLifecycleTests(unittest.TestCase):
    def test_end_to_end_sale_inventory_payment_and_reports_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "company.sqlite"
            app = Copperline(path)
            boot = app.bootstrap(company_name="Lifecycle Co")
            accounts = {a.code: a for a in app.accounts.list(active=None)}
            warehouse = app.inventory.create_warehouse("MAIN", "Main Warehouse")
            item = app.inventory.create_item(
                "SKU-1",
                "Sellable Product",
                "inventory",
                "USD",
                accounts["4000"].id,
                accounts["5000"].id,
                inventory_account_id=accounts["1200"].id,
                default_price=Money("30", "USD"),
            )
            app.inventory.receive(item.id, warehouse.id, "2026-01-01", 10, Money("10", "USD"))
            customer = app.parties.create(
                "Lifecycle Customer",
                "customer",
                "USD",
                receivable_account_id=accounts["1100"].id,
            )
            invoice = app.invoices.create(
                customer.id,
                "2026-01-05",
                [
                    InvoiceLine(
                        "Product",
                        2,
                        Money("30", "USD"),
                        accounts["4000"].id,
                        item_id=item.id,
                    )
                ],
            )
            invoice = app.invoices.issue(invoice.id)
            movement, cogs = app.inventory.issue(
                item.id, warehouse.id, "2026-01-05", 2, source_id=invoice.id
            )
            self.assertEqual(cogs, Money("20", "USD"))
            payment = app.payments.record(
                customer.id,
                "2026-01-10",
                Money("60", "USD"),
                accounts["1010"].id,
                kind="receipt",
                allocations=[PaymentAllocation(invoice.id, Money("60", "USD"))],
            )
            self.assertEqual(app.invoices.outstanding(invoice.id), Money.zero("USD"))
            app.close()

            reopened = Copperline(path)
            try:
                self.assertEqual(reopened.invoices.get(invoice.id).status.value, "paid")
                self.assertEqual(reopened.inventory.quantity_on_hand(item.id, warehouse.id), 8)
                pnl = reopened.reports.profit_and_loss(
                    start="2026-01-01", end="2026-01-31"
                )
                self.assertEqual(pnl["revenue_total"]["amount"], "60.00")
                self.assertEqual(pnl["expense_total"]["amount"], "20.00")
                self.assertEqual(pnl["net_income"]["amount"], "40.00")
                self.assertEqual(
                    reopened.ledger.balance(accounts["1010"].id),
                    Money("60", "USD"),
                )
                self.assertGreater(reopened.audit.count(), 5)
            finally:
                reopened.close()

    def test_period_lock_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "company.sqlite"
            app = Copperline(path)
            app.bootstrap(company_name="Locked Co")
            app.periods.define("2026-01-01", "2026-01-31")
            app.periods.lock("2026-01-01")
            app.close()
            reopened = Copperline(path)
            try:
                self.assertTrue(reopened.periods.period_for("2026-01-15")["locked"])
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
