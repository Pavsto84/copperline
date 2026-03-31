from __future__ import annotations

import unittest
from datetime import date

from copperline.errors import ConflictError, PeriodLockedError, UnbalancedEntryError, ValidationError
from copperline.models import JournalLine
from copperline.money import Money

from tests.support import AppFixture


class AccountsAndLedgerTests(unittest.TestCase):
    def setUp(self):
        self.fx = AppFixture()

    def tearDown(self):
        self.fx.close()

    def test_bootstrap_creates_balanced_chart_types(self):
        types = {account.type.value for account in self.fx.app.accounts.list(active=None)}
        self.assertEqual(types, {"asset", "liability", "equity", "revenue", "expense"})

    def test_account_code_must_be_unique(self):
        with self.assertRaises(ConflictError):
            self.fx.app.accounts.create("1000", "Duplicate", "asset", "USD")

    def test_child_must_match_parent_type_and_currency(self):
        parent = self.fx.account("1000")
        child = self.fx.app.accounts.create("1001", "Petty cash", "asset", "USD", parent_id=parent.id)
        self.assertEqual(child.parent_id, parent.id)
        with self.assertRaises(ValidationError):
            self.fx.app.accounts.create("1002", "Bad child", "expense", "USD", parent_id=parent.id)

    def test_deactivate_rejects_active_children(self):
        parent = self.fx.account("1000")
        self.fx.app.accounts.create("1001", "Child", "asset", "USD", parent_id=parent.id)
        with self.assertRaises(ConflictError):
            self.fx.app.accounts.deactivate(parent.id)

    def test_descendants_are_recursive(self):
        parent = self.fx.account("1000")
        child = self.fx.app.accounts.create("1001", "Child", "asset", "USD", parent_id=parent.id)
        grand = self.fx.app.accounts.create("1002", "Grand", "asset", "USD", parent_id=child.id)
        self.assertEqual([a.id for a in self.fx.app.accounts.descendants(parent.id)], [child.id, grand.id])

    def test_unbalanced_journal_is_rejected(self):
        with self.assertRaises(UnbalancedEntryError):
            self.fx.app.ledger.draft(
                "2026-01-01",
                "bad",
                "USD",
                [
                    JournalLine(
                        account_id=self.fx.account("1000").id,
                        debit=Money("10", "USD"),
                        credit=Money.zero("USD"),
                    ),
                    JournalLine(
                        account_id=self.fx.account("4000").id,
                        debit=Money.zero("USD"),
                        credit=Money("9", "USD"),
                    ),
                ],
            )

    def test_posted_entry_updates_normal_balances(self):
        self.fx.post_cash_sale("125.00")
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("1000").id), Money("125", "USD"))
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("4000").id), Money("125", "USD"))

    def test_raw_debit_credit_preserves_sides(self):
        self.fx.post_cash_sale("125.00")
        debit, credit = self.fx.app.ledger.raw_debit_credit(self.fx.account("1000").id)
        self.assertEqual(debit, Money("125", "USD"))
        self.assertEqual(credit, Money.zero("USD"))

    def test_entry_roundtrips_lines(self):
        entry = self.fx.post_cash_sale("50")
        loaded = self.fx.app.ledger.get(entry.id)
        self.assertEqual(loaded.id, entry.id)
        self.assertEqual(len(loaded.lines), 2)
        self.assertEqual(loaded.lines[0].debit + loaded.lines[1].debit, Money("50", "USD"))

    def test_reverse_restores_account_balance(self):
        entry = self.fx.post_cash_sale("50")
        reversal = self.fx.app.ledger.reverse(entry.id, "2026-01-11", reason="mistake")
        self.assertEqual(reversal.source, "reversal")
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("1000").id), Money.zero("USD"))

    def test_draft_can_be_voided(self):
        entry = self.fx.app.ledger.draft(
            "2026-01-01",
            "draft",
            "USD",
            [
                JournalLine(self.fx.account("1000").id, Money("1", "USD"), Money.zero("USD")),
                JournalLine(self.fx.account("3000").id, Money.zero("USD"), Money("1", "USD")),
            ],
        )
        voided = self.fx.app.ledger.void_draft(entry.id)
        self.assertEqual(voided.status.value, "void")

    def test_posted_entry_cannot_be_voided(self):
        entry = self.fx.post_cash_sale("1")
        with self.assertRaises(ConflictError):
            self.fx.app.ledger.void_draft(entry.id)

    def test_period_lock_blocks_posting(self):
        self.fx.app.periods.define("2026-01-01", "2026-01-31")
        self.fx.app.periods.lock("2026-01-01")
        with self.assertRaises(PeriodLockedError):
            self.fx.post_cash_sale("1", "2026-01-20")

    def test_period_lock_rejects_existing_draft(self):
        self.fx.app.periods.define("2026-01-01", "2026-01-31")
        self.fx.app.ledger.draft(
            "2026-01-05",
            "draft",
            "USD",
            [
                JournalLine(self.fx.account("1000").id, Money("1", "USD"), Money.zero("USD")),
                JournalLine(self.fx.account("3000").id, Money.zero("USD"), Money("1", "USD")),
            ],
        )
        with self.assertRaises(ConflictError):
            self.fx.app.periods.lock("2026-01-01")

    def test_period_unlock_requires_reason(self):
        self.fx.app.periods.define("2026-01-01", "2026-01-31")
        self.fx.app.periods.lock("2026-01-01")
        with self.assertRaises(ValidationError):
            self.fx.app.periods.unlock("2026-01-01", actor="admin", reason="")

    def test_period_overlap_is_rejected(self):
        self.fx.app.periods.define("2026-01-01", "2026-01-31")
        with self.assertRaises(ConflictError):
            self.fx.app.periods.define("2026-01-15", "2026-02-15")

    def test_trial_balance_balances(self):
        self.fx.post_cash_sale("25")
        report = self.fx.app.reports.trial_balance(as_of="2026-01-31")
        self.assertTrue(report["balanced"])
        self.assertEqual(report["debit_total"]["amount"], "25.00")
        self.assertEqual(report["credit_total"]["amount"], "25.00")

    def test_idempotent_operation_replays_result(self):
        calls = []
        first = self.fx.app.ledger.idempotent(
            "demo",
            "key-1",
            {"amount": 1},
            lambda: calls.append(1) or {"result": "ok"},
        )
        second = self.fx.app.ledger.idempotent(
            "demo",
            "key-1",
            {"amount": 1},
            lambda: calls.append(2) or {"result": "different"},
        )
        self.assertEqual(first, second)
        self.assertEqual(calls, [1])

    def test_idempotency_rejects_payload_change(self):
        self.fx.app.ledger.idempotent("demo", "key-1", {"amount": 1}, lambda: {"ok": True})
        with self.assertRaises(ConflictError):
            self.fx.app.ledger.idempotent("demo", "key-1", {"amount": 2}, lambda: {"ok": True})

    def test_nested_database_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.fx.app.db.transaction():
                self.fx.app.db.set_setting("x", 1)
                with self.fx.app.db.transaction():
                    self.fx.app.db.set_setting("y", 2)
                raise RuntimeError("stop")
        self.assertIsNone(self.fx.app.db.get_setting("x"))
        self.assertIsNone(self.fx.app.db.get_setting("y"))


if __name__ == "__main__":
    unittest.main()
