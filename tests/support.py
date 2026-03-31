from __future__ import annotations

import tempfile
from pathlib import Path

from copperline import Copperline
from copperline.models import InvoiceLine, JournalLine, PartyKind
from copperline.money import Money


class AppFixture:
    def __init__(self, *, file_db: bool = False) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="copperline-tests-")
        self.path = (
            str(Path(self.tempdir.name) / "copperline.sqlite")
            if file_db
            else ":memory:"
        )
        self.app = Copperline(self.path)
        self.bootstrap = self.app.bootstrap(company_name="Test Company", currency="USD")
        self.by_code = {
            account.code: account
            for account in self.app.accounts.list(active=None)
        }

    def close(self) -> None:
        self.app.close()
        self.tempdir.cleanup()

    def account(self, code: str):
        return self.by_code[code]

    def customer(self, name: str = "Acme Customer"):
        return self.app.parties.create(
            name,
            PartyKind.CUSTOMER,
            "USD",
            receivable_account_id=self.account("1100").id,
        )

    def vendor(self, name: str = "Supply Vendor"):
        return self.app.parties.create(
            name,
            PartyKind.VENDOR,
            "USD",
            payable_account_id=self.account("2000").id,
        )

    def sales_tax(self, code: str = "TAX10"):
        return self.app.taxes.create(
            code,
            "Sales Tax 10%",
            "10",
            self.account("2100").id,
        )

    def purchase_tax(self, code: str = "INPUT10"):
        return self.app.taxes.create(
            code,
            "Input Tax 10%",
            "10",
            self.account("1300").id,
            recoverable_percent=100,
        )

    def posted_sale_invoice(
        self,
        amount: str = "100.00",
        *,
        tax_code: str | None = None,
        party=None,
        day: str = "2026-01-10",
    ):
        party = party or self.customer()
        invoice = self.app.invoices.create(
            party.id,
            day,
            [
                InvoiceLine(
                    description="Consulting",
                    quantity="1",
                    unit_price=Money(amount, "USD"),
                    account_id=self.account("4000").id,
                    tax_code=tax_code,
                )
            ],
            kind="sale",
        )
        return self.app.invoices.issue(invoice.id)

    def post_cash_sale(self, amount: str, day: str = "2026-01-10"):
        value = Money(amount, "USD")
        return self.app.ledger.create_and_post(
            day,
            "Cash sale",
            "USD",
            [
                JournalLine(
                    account_id=self.account("1000").id,
                    debit=value,
                    credit=Money.zero("USD"),
                ),
                JournalLine(
                    account_id=self.account("4000").id,
                    debit=Money.zero("USD"),
                    credit=value,
                ),
            ],
        )
