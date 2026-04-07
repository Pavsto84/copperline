from __future__ import annotations

import unittest

from copperline.errors import ConflictError, ValidationError
from copperline.models import InvoiceLine, PaymentAllocation
from copperline.money import Money
from tests.support import AppFixture


class InvoicePaymentTaxTests(unittest.TestCase):
    def setUp(self):
        self.fx = AppFixture()

    def tearDown(self):
        self.fx.close()

    def test_exclusive_tax_invoice_posts_receivable_revenue_tax(self):
        customer = self.fx.customer()
        self.fx.sales_tax()
        invoice = self.fx.posted_sale_invoice("100", tax_code="TAX10", party=customer)
        totals = self.fx.app.invoices.totals(invoice)
        self.assertEqual(totals["net"], Money("100", "USD"))
        self.assertEqual(totals["tax"], Money("10", "USD"))
        self.assertEqual(totals["gross"], Money("110", "USD"))
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("1100").id), Money("110", "USD"))
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("4000").id), Money("100", "USD"))
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("2100").id), Money("10", "USD"))

    def test_inclusive_tax_split(self):
        customer = self.fx.customer()
        self.fx.app.taxes.create("INC10", "Inclusive 10", "10", self.fx.account("2100").id, inclusive=True)
        invoice = self.fx.app.invoices.create(
            customer.id,
            "2026-01-01",
            [InvoiceLine("Inclusive", "1", Money("110", "USD"), self.fx.account("4000").id, tax_code="INC10")],
        )
        totals = self.fx.app.invoices.totals(invoice)
        self.assertEqual(totals["net"], Money("100", "USD"))
        self.assertEqual(totals["tax"], Money("10", "USD"))

    def test_invoice_discount_reduces_tax_base(self):
        customer = self.fx.customer()
        self.fx.sales_tax()
        invoice = self.fx.app.invoices.create(
            customer.id,
            "2026-01-01",
            [InvoiceLine("Discounted", "2", Money("50", "USD"), self.fx.account("4000").id, tax_code="TAX10", discount_percent="20")],
        )
        totals = self.fx.app.invoices.totals(invoice)
        self.assertEqual(totals["net"], Money("80", "USD"))
        self.assertEqual(totals["tax"], Money("8", "USD"))

    def test_purchase_invoice_uses_payable_and_expense(self):
        vendor = self.fx.vendor()
        invoice = self.fx.app.invoices.create(
            vendor.id,
            "2026-01-02",
            [InvoiceLine("Rent", "1", Money("250", "USD"), self.fx.account("6000").id)],
            kind="purchase",
        )
        self.fx.app.invoices.issue(invoice.id)
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("2000").id), Money("250", "USD"))
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("6000").id), Money("250", "USD"))

    def test_purchase_recoverable_tax_posts_asset(self):
        vendor = self.fx.vendor()
        self.fx.purchase_tax()
        invoice = self.fx.app.invoices.create(
            vendor.id,
            "2026-01-02",
            [InvoiceLine("Supplies", "1", Money("100", "USD"), self.fx.account("6000").id, tax_code="INPUT10")],
            kind="purchase",
        )
        self.fx.app.invoices.issue(invoice.id)
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("1300").id), Money("10", "USD"))
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("2000").id), Money("110", "USD"))

    def test_receipt_allocation_marks_invoice_paid(self):
        invoice = self.fx.posted_sale_invoice("100")
        customer = self.fx.app.parties.get(invoice.party_id)
        payment = self.fx.app.payments.record(
            customer.id,
            "2026-01-15",
            Money("100", "USD"),
            self.fx.account("1010").id,
            kind="receipt",
            allocations=[PaymentAllocation(invoice.id, Money("100", "USD"))],
        )
        self.assertEqual(self.fx.app.invoices.get(invoice.id).status.value, "paid")
        self.assertEqual(self.fx.app.invoices.outstanding(invoice.id), Money.zero("USD"))
        self.assertEqual(self.fx.app.payments.unallocated(payment.id), Money.zero("USD"))

    def test_partial_receipt_marks_partially_paid(self):
        invoice = self.fx.posted_sale_invoice("100")
        customer = self.fx.app.parties.get(invoice.party_id)
        self.fx.app.payments.record(
            customer.id,
            "2026-01-15",
            Money("30", "USD"),
            self.fx.account("1010").id,
            kind="receipt",
            allocations=[PaymentAllocation(invoice.id, Money("30", "USD"))],
        )
        loaded = self.fx.app.invoices.get(invoice.id)
        self.assertEqual(loaded.status.value, "partially_paid")
        self.assertEqual(self.fx.app.invoices.outstanding(invoice.id), Money("70", "USD"))

    def test_allocation_cannot_exceed_invoice(self):
        invoice = self.fx.posted_sale_invoice("100")
        customer = self.fx.app.parties.get(invoice.party_id)
        with self.assertRaises(ValidationError):
            self.fx.app.payments.record(
                customer.id,
                "2026-01-15",
                Money("200", "USD"),
                self.fx.account("1010").id,
                kind="receipt",
                allocations=[PaymentAllocation(invoice.id, Money("101", "USD"))],
            )

    def test_auto_allocate_oldest_due_first(self):
        customer = self.fx.customer()
        first = self.fx.posted_sale_invoice("50", party=customer, day="2026-01-01")
        second = self.fx.posted_sale_invoice("70", party=customer, day="2026-01-02")
        allocations = self.fx.app.payments.auto_allocate(customer.id, Money("80", "USD"), kind="receipt")
        self.assertEqual(allocations[0].invoice_id, first.id)
        self.assertEqual(allocations[0].amount, Money("50", "USD"))
        self.assertEqual(allocations[1].invoice_id, second.id)
        self.assertEqual(allocations[1].amount, Money("30", "USD"))

    def test_credit_note_reverses_revenue(self):
        invoice = self.fx.posted_sale_invoice("100")
        note = self.fx.app.invoices.credit_note(invoice.id, actor="tester")
        self.assertEqual(note.credit_of_invoice_id, invoice.id)
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("4000").id), Money.zero("USD"))
        self.assertEqual(self.fx.app.ledger.balance(self.fx.account("1100").id), Money.zero("USD"))

    def test_draft_invoice_can_be_voided(self):
        customer = self.fx.customer()
        invoice = self.fx.app.invoices.create(
            customer.id,
            "2026-01-01",
            [InvoiceLine("x", 1, Money("10", "USD"), self.fx.account("4000").id)],
        )
        self.assertEqual(self.fx.app.invoices.void_draft(invoice.id).status.value, "void")

    def test_issued_invoice_cannot_be_directly_voided(self):
        invoice = self.fx.posted_sale_invoice("10")
        with self.assertRaises(ConflictError):
            self.fx.app.invoices.void_draft(invoice.id)

    def test_tax_summary_reports_payable(self):
        self.fx.sales_tax()
        self.fx.posted_sale_invoice("100", tax_code="TAX10")
        report = self.fx.app.reports.tax_summary(start="2026-01-01", end="2026-01-31")
        row = next(row for row in report["rows"] if row["tax_code"] == "TAX10")
        self.assertEqual(row["net_payable"]["amount"], "10.00")

    def test_party_search(self):
        self.fx.customer("Acme Incorporated")
        self.fx.vendor("Northwind Supplies")
        self.assertEqual(len(self.fx.app.parties.list(search="acme")), 1)
        self.assertEqual(len(self.fx.app.parties.list(kind="vendor")), 1)


if __name__ == "__main__":
    unittest.main()
