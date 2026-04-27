"""Portable data exports for accountants, auditors, and backups."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any, Iterable

from .accounts import AccountService
from .audit import AuditService
from .banking import BankingService
from .budget import BudgetService
from .db import Database
from .invoices import InvoiceService
from .inventory import InventoryService
from .jsonutil import dumps
from .ledger import LedgerService
from .parties import PartyService
from .payments import PaymentService
from .timeutil import parse_date


class ExportService:
    def __init__(
        self,
        db: Database,
        *,
        accounts: AccountService,
        ledger: LedgerService,
        parties: PartyService,
        invoices: InvoiceService,
        payments: PaymentService,
        inventory: InventoryService,
        banking: BankingService,
        budgets: BudgetService,
        audit: AuditService,
    ) -> None:
        self.db = db
        self.accounts = accounts
        self.ledger = ledger
        self.parties = parties
        self.invoices = invoices
        self.payments = payments
        self.inventory = inventory
        self.banking = banking
        self.budgets = budgets
        self.audit = audit

    def journal_csv(
        self,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
    ) -> str:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            [
                "entry_id",
                "date",
                "status",
                "reference",
                "description",
                "line_no",
                "account_code",
                "account_name",
                "debit",
                "credit",
                "currency",
                "memo",
                "party_id",
                "item_id",
                "tax_code",
                "dimensions",
            ]
        )
        for entry in reversed(
            self.ledger.list(start=start, end=end, limit=100000)
        ):
            for line_no, line in enumerate(entry.lines, start=1):
                account = self.accounts.get(line.account_id)
                writer.writerow(
                    [
                        entry.id,
                        entry.date.isoformat(),
                        entry.status.value,
                        entry.reference,
                        entry.description,
                        line_no,
                        account.code,
                        account.name,
                        format(line.debit.amount, "f"),
                        format(line.credit.amount, "f"),
                        entry.currency,
                        line.memo,
                        line.party_id or "",
                        line.item_id or "",
                        line.tax_code or "",
                        dumps(line.dimensions),
                    ]
                )
        return output.getvalue()

    def general_ledger_csv(
        self,
        account_id: str,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
    ) -> str:
        account = self.accounts.get(account_id)
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            [
                "date",
                "entry_id",
                "reference",
                "description",
                "memo",
                "debit",
                "credit",
                "running_balance",
                "currency",
            ]
        )
        entries = list(
            reversed(
                self.ledger.list(
                    start=start,
                    end=end,
                    status="posted",
                    account_id=account.id,
                    limit=100000,
                )
            )
        )
        running = 0
        for entry in entries:
            for line in entry.lines:
                if line.account_id != account.id:
                    continue
                delta = line.debit.minor_units - line.credit.minor_units
                if account.normal_balance.value == "credit":
                    delta = -delta
                running += delta
                writer.writerow(
                    [
                        entry.date.isoformat(),
                        entry.id,
                        entry.reference,
                        entry.description,
                        line.memo,
                        format(line.debit.amount, "f"),
                        format(line.credit.amount, "f"),
                        format(
                            line.debit.from_minor(running, account.currency).amount,
                            "f",
                        ),
                        account.currency,
                    ]
                )
        return output.getvalue()

    def invoices_csv(self, *, kind: str | None = None) -> str:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            [
                "invoice_id",
                "number",
                "kind",
                "party",
                "issue_date",
                "due_date",
                "status",
                "net",
                "tax",
                "gross",
                "paid",
                "outstanding",
                "currency",
            ]
        )
        for invoice in self.invoices.list(kind=kind, limit=100000):
            party = self.parties.get(invoice.party_id)
            totals = self.invoices.totals(invoice)
            paid = self.invoices.paid_amount(invoice.id)
            outstanding = totals["gross"] - paid
            writer.writerow(
                [
                    invoice.id,
                    invoice.number,
                    invoice.kind,
                    party.name,
                    invoice.issue_date.isoformat(),
                    invoice.due_date.isoformat(),
                    invoice.status.value,
                    format(totals["net"].amount, "f"),
                    format(totals["tax"].amount, "f"),
                    format(totals["gross"].amount, "f"),
                    format(paid.amount, "f"),
                    format(outstanding.amount, "f"),
                    invoice.currency,
                ]
            )
        return output.getvalue()

    def inventory_csv(self) -> str:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            [
                "sku",
                "item_name",
                "warehouse_code",
                "quantity",
                "value",
                "average_unit_cost",
                "currency",
            ]
        )
        for warehouse in self.inventory.list_warehouses(active=None):
            for item in self.inventory.list_items(kind="inventory", active=None):
                quantity = self.inventory.quantity_on_hand(item.id, warehouse.id)
                value = self.inventory.valuation(item.id, warehouse.id)
                if quantity == 0 and value.is_zero():
                    continue
                average = (
                    self.inventory.average_unit_cost(item.id, warehouse.id)
                    if quantity
                    else value.zero(item.currency)
                )
                writer.writerow(
                    [
                        item.sku,
                        item.name,
                        warehouse.code,
                        format(quantity, "f"),
                        format(value.amount, "f"),
                        format(average.amount, "f"),
                        item.currency,
                    ]
                )
        return output.getvalue()

    def audit_jsonl(self, *, after_seq: int = 0) -> str:
        records = self.audit.list(limit=5000, after_seq=after_seq)
        return "".join(dumps(record) + "\n" for record in records)

    def snapshot_json(self) -> str:
        payload = {
            "format": "copperline-snapshot-v1",
            "accounts": [self.accounts.to_dict(item) for item in self.accounts.list(active=None)],
            "parties": [self.parties.to_dict(item) for item in self.parties.list(limit=100000)],
            "journal_entries": [
                self.ledger.entry_dict(item)
                for item in reversed(self.ledger.list(limit=100000))
            ],
            "invoices": [
                self.invoices.to_dict(item)
                for item in self.invoices.list(limit=100000)
            ],
            "payments": [
                self.payments.to_dict(item)
                for item in self.payments.list(limit=100000)
            ],
            "items": [
                self.inventory.item_dict(item)
                for item in self.inventory.list_items(active=None)
            ],
            "warehouses": [
                {
                    "id": item.id,
                    "code": item.code,
                    "name": item.name,
                    "active": item.active,
                }
                for item in self.inventory.list_warehouses(active=None)
            ],
            "bank_accounts": [
                self.banking.account_dict(item)
                for item in self.banking.list_accounts()
            ],
            "budgets": [
                self.budgets.to_dict(item)
                for item in self.budgets.list(active=None)
            ],
            "audit": self.audit.list(limit=5000),
        }
        return dumps(payload, pretty=True)
