from __future__ import annotations

import unittest
from decimal import Decimal

from copperline.errors import ConflictError, ReconciliationError
from copperline.models import JournalLine
from copperline.money import Money
from tests.support import AppFixture


class BankingReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.fx = AppFixture()
        self.bank = self.fx.app.banking.list_accounts()[0]

    def tearDown(self):
        self.fx.close()

    def post_bank_sale(self, amount: str, day: str = "2026-01-10"):
        value = Money(amount, "USD")
        return self.fx.app.ledger.create_and_post(
            day,
            "Bank sale",
            "USD",
            [
                JournalLine(
                    self.fx.account("1010").id,
                    value,
                    Money.zero("USD"),
                ),
                JournalLine(
                    self.fx.account("4000").id,
                    Money.zero("USD"),
                    value,
                ),
            ],
        )

    def test_import_is_idempotent_by_external_id(self):
        txns = [{"posted_date": "2026-01-10", "amount": Money("25", "USD"), "description": "Sale", "external_id": "x1"}]
        first = self.fx.app.banking.import_transactions(self.bank.id, txns)
        second = self.fx.app.banking.import_transactions(self.bank.id, txns)
        self.assertEqual(len(first["inserted"]), 1)
        self.assertEqual(second["duplicates"], ["x1"])

    def test_candidate_exact_amount_scores_high(self):
        entry = self.post_bank_sale("25", "2026-01-10")
        imported = self.fx.app.banking.import_transactions(
            self.bank.id,
            [{"posted_date": "2026-01-10", "amount": Money("25", "USD"), "description": "cash sale", "external_id": "x1"}],
        )
        txn = imported["inserted"][0]
        candidates = self.fx.app.reconciliation.candidates(txn)
        self.assertEqual(candidates[0].entry_id, entry.id)
        self.assertGreaterEqual(candidates[0].score, Decimal("0.9"))

    def test_match_marks_transaction(self):
        entry = self.post_bank_sale("25", "2026-01-10")
        txn = self.fx.app.banking.import_transactions(
            self.bank.id,
            [{"posted_date": "2026-01-10", "amount": Money("25", "USD"), "description": "cash sale", "external_id": "x1"}],
        )["inserted"][0]
        matched = self.fx.app.reconciliation.match(txn, entry.id)
        self.assertEqual(matched.status.value, "matched")
        self.assertEqual(matched.matched_entry_id, entry.id)

    def test_mismatched_amount_rejected(self):
        entry = self.post_bank_sale("25", "2026-01-10")
        txn = self.fx.app.banking.import_transactions(
            self.bank.id,
            [{"posted_date": "2026-01-10", "amount": Money("24", "USD"), "description": "cash sale", "external_id": "x1"}],
        )["inserted"][0]
        with self.assertRaises(ReconciliationError):
            self.fx.app.reconciliation.match(txn, entry.id)

    def test_entry_cannot_match_two_transactions(self):
        entry = self.post_bank_sale("25", "2026-01-10")
        ids = self.fx.app.banking.import_transactions(
            self.bank.id,
            [
                {"posted_date": "2026-01-10", "amount": Money("25", "USD"), "description": "a", "external_id": "a"},
                {"posted_date": "2026-01-10", "amount": Money("25", "USD"), "description": "b", "external_id": "b"},
            ],
        )["inserted"]
        self.fx.app.reconciliation.match(ids[0], entry.id)
        with self.assertRaises(ConflictError):
            self.fx.app.reconciliation.match(ids[1], entry.id)

    def test_unmatch_allows_reconciliation_again(self):
        entry = self.post_bank_sale("25", "2026-01-10")
        txn = self.fx.app.banking.import_transactions(
            self.bank.id,
            [{"posted_date": "2026-01-10", "amount": Money("25", "USD"), "description": "sale", "external_id": "a"}],
        )["inserted"][0]
        self.fx.app.reconciliation.match(txn, entry.id)
        self.assertEqual(self.fx.app.banking.unmatch(txn).status.value, "unmatched")

    def test_ignore_requires_reason(self):
        txn = self.fx.app.banking.import_transactions(
            self.bank.id,
            [{"posted_date": "2026-01-10", "amount": Money("1", "USD"), "description": "fee", "external_id": "a"}],
        )["inserted"][0]
        with self.assertRaises(Exception):
            self.fx.app.banking.mark_ignored(txn, reason="")
        self.assertEqual(self.fx.app.banking.mark_ignored(txn, reason="duplicate").status.value, "ignored")

    def test_auto_match_exact_candidate(self):
        entry = self.post_bank_sale("25", "2026-01-10")
        txn = self.fx.app.banking.import_transactions(
            self.bank.id,
            [{"posted_date": "2026-01-10", "amount": Money("25", "USD"), "description": "Cash sale", "external_id": "a"}],
        )["inserted"][0]
        result = self.fx.app.reconciliation.auto_match(self.bank.id, threshold="0.8")
        self.assertEqual(result["matched"][0]["transaction_id"], txn)
        self.assertEqual(result["matched"][0]["entry_id"], entry.id)

    def test_reconciliation_session_difference(self):
        self.post_bank_sale("25", "2026-01-10")
        session = self.fx.app.reconciliation.start_session(self.bank.id, "2026-01-31", Money("25", "USD"))
        status = self.fx.app.reconciliation.session_status(session)
        self.assertEqual(status["difference"]["amount"], "0.00")

    def test_complete_session_requires_zero_difference(self):
        session = self.fx.app.reconciliation.start_session(self.bank.id, "2026-01-31", Money("1", "USD"))
        with self.assertRaises(ReconciliationError):
            self.fx.app.reconciliation.complete_session(session, require_all_matched=False)

    def test_complete_session_can_finish_balanced_empty_statement(self):
        session = self.fx.app.reconciliation.start_session(self.bank.id, "2026-01-31", Money.zero("USD"))
        result = self.fx.app.reconciliation.complete_session(session)
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
