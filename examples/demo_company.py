from __future__ import annotations

from pathlib import Path

from copperline import Copperline, Money
from copperline.models import InvoiceLine, PaymentAllocation

path = Path("demo.sqlite")

with Copperline(path) as app:
    if not app.accounts.list(active=None):
        app.bootstrap(company_name="Copperline Demo", currency="USD")

    accounts = {account.code: account for account in app.accounts.list(active=None)}

    customer = app.parties.create(
        "Example Customer",
        "customer",
        "USD",
        receivable_account_id=accounts["1100"].id,
    )

    tax = app.taxes.create(
        "TAX10",
        "Sales Tax 10%",
        "10",
        accounts["2100"].id,
    )

    invoice = app.invoices.create(
        customer.id,
        "2026-09-05",
        [
            InvoiceLine(
                description="Professional services",
                quantity=2,
                unit_price=Money("150", "USD"),
                account_id=accounts["4000"].id,
                tax_code=tax.code,
            )
        ],
    )
    invoice = app.invoices.issue(invoice.id)

    app.payments.record(
        customer.id,
        "2026-09-10",
        Money("330", "USD"),
        accounts["1010"].id,
        kind="receipt",
        allocations=[PaymentAllocation(invoice.id, Money("330", "USD"))],
    )

    print(app.reports.profit_and_loss(start="2026-09-01", end="2026-09-30"))
