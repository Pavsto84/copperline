from __future__ import annotations

import unittest

from copperline.errors import ConflictError, InsufficientStockError
from copperline.money import Money
from tests.support import AppFixture


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.fx = AppFixture()
        self.wh1 = self.fx.app.inventory.create_warehouse("MAIN", "Main")
        self.wh2 = self.fx.app.inventory.create_warehouse("EAST", "East")
        self.item = self.fx.app.inventory.create_item(
            "WIDGET",
            "Widget",
            "inventory",
            "USD",
            self.fx.account("4000").id,
            self.fx.account("5000").id,
            inventory_account_id=self.fx.account("1200").id,
            default_price=Money("25", "USD"),
        )

    def tearDown(self):
        self.fx.close()

    def test_receive_adds_quantity_and_value(self):
        self.fx.app.inventory.receive(self.item.id, self.wh1.id, "2026-01-01", 10, Money("4", "USD"))
        self.assertEqual(self.fx.app.inventory.quantity_on_hand(self.item.id, self.wh1.id), 10)
        self.assertEqual(self.fx.app.inventory.valuation(self.item.id, self.wh1.id), Money("40", "USD"))

    def test_fifo_issue_consumes_oldest_layer(self):
        self.fx.app.inventory.receive(self.item.id, self.wh1.id, "2026-01-01", 5, Money("4", "USD"))
        self.fx.app.inventory.receive(self.item.id, self.wh1.id, "2026-01-02", 5, Money("6", "USD"))
        movement, cost = self.fx.app.inventory.issue(self.item.id, self.wh1.id, "2026-01-03", 6)
        self.assertEqual(cost, Money("26", "USD"))
        self.assertEqual(movement.quantity, -6)
        layers = self.fx.app.inventory.layers(self.item.id, self.wh1.id)
        self.assertEqual(layers[0].remaining_quantity, 0)
        self.assertEqual(layers[1].remaining_quantity, 4)

    def test_issue_posts_cogs_and_reduces_inventory_asset(self):
        self.fx.app.inventory.receive(self.item.id, self.wh1.id, "2026-01-01", 10, Money("4", "USD"))
        self.fx.app.inventory.issue(self.item.id, self.wh1.id, "2026-01-02", 3)
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("1200").id), Money("28", "USD"))
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("5000").id), Money("12", "USD"))

    def test_insufficient_stock_is_rejected(self):
        with self.assertRaises(InsufficientStockError):
            self.fx.app.inventory.issue(self.item.id, self.wh1.id, "2026-01-02", 1)

    def test_transfer_preserves_total_value(self):
        self.fx.app.inventory.receive(self.item.id, self.wh1.id, "2026-01-01", 10, Money("4", "USD"))
        before = self.fx.app.inventory.valuation(self.item.id)
        self.fx.app.inventory.transfer(self.item.id, self.wh1.id, self.wh2.id, "2026-01-02", 3)
        self.assertEqual(self.fx.app.inventory.quantity_on_hand(self.item.id, self.wh1.id), 7)
        self.assertEqual(self.fx.app.inventory.quantity_on_hand(self.item.id, self.wh2.id), 3)
        self.assertEqual(self.fx.app.inventory.valuation(self.item.id), before)

    def test_transfer_same_warehouse_rejected(self):
        with self.assertRaises(Exception):
            self.fx.app.inventory.transfer(self.item.id, self.wh1.id, self.wh1.id, "2026-01-02", 1)

    def test_positive_adjustment(self):
        movement, value = self.fx.app.inventory.adjust(
            self.item.id, self.wh1.id, "2026-01-01", 2, unit_cost=Money("3", "USD"), reason="count"
        )
        self.assertEqual(movement.quantity, 2)
        self.assertEqual(value, Money("6", "USD"))

    def test_negative_adjustment(self):
        self.fx.app.inventory.receive(self.item.id, self.wh1.id, "2026-01-01", 5, Money("3", "USD"))
        movement, value = self.fx.app.inventory.adjust(
            self.item.id, self.wh1.id, "2026-01-02", -2, reason="damage"
        )
        self.assertEqual(movement.quantity, -2)
        self.assertEqual(value, Money("6", "USD"))

    def test_cannot_deactivate_item_with_stock(self):
        self.fx.app.inventory.receive(self.item.id, self.wh1.id, "2026-01-01", 1, Money("3", "USD"))
        with self.assertRaises(ConflictError):
            self.fx.app.inventory.deactivate_item(self.item.id)

    def test_inventory_report_groups_warehouse(self):
        self.fx.app.inventory.receive(self.item.id, self.wh1.id, "2026-01-01", 2, Money("3", "USD"))
        report = self.fx.app.reports.inventory_valuation(warehouse_id=self.wh1.id)
        self.assertEqual(report["rows"][0]["quantity"], "2")
        self.assertEqual(report["rows"][0]["value"]["amount"], "6.00")

    def test_moving_average_costing(self):
        avg_item = self.fx.app.inventory.create_item(
            "AVG", "Average", "inventory", "USD",
            self.fx.account("4000").id, self.fx.account("5000").id,
            inventory_account_id=self.fx.account("1200").id,
            cost_method="moving_average",
        )
        self.fx.app.inventory.receive(avg_item.id, self.wh1.id, "2026-01-01", 5, Money("2", "USD"))
        self.fx.app.inventory.receive(avg_item.id, self.wh1.id, "2026-01-02", 5, Money("4", "USD"))
        _movement, cost = self.fx.app.inventory.issue(avg_item.id, self.wh1.id, "2026-01-03", 2)
        self.assertEqual(cost, Money("6", "USD"))

    def test_as_of_quantity(self):
        self.fx.app.inventory.receive(self.item.id, self.wh1.id, "2026-01-01", 5, Money("2", "USD"))
        self.fx.app.inventory.issue(self.item.id, self.wh1.id, "2026-01-05", 2)
        self.assertEqual(self.fx.app.inventory.quantity_on_hand(self.item.id, self.wh1.id, as_of="2026-01-03"), 5)
        self.assertEqual(self.fx.app.inventory.quantity_on_hand(self.item.id, self.wh1.id, as_of="2026-01-06"), 3)


if __name__ == "__main__":
    unittest.main()
