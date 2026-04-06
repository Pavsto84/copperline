"""Customer/vendor payments, allocations, and control-account posting."""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .audit import AuditService
from .db import Database
from .errors import ConflictError, CurrencyMismatchError, NotFoundError, ValidationError
from .ids import new_id
from .invoices import InvoiceService
from .ledger import LedgerService
from .models import InvoiceStatus, JournalLine, Payment, PaymentAllocation
from .money import Money
from .parties import PartyService
from .settings import SettingsService
from .timeutil import parse_date


class PaymentService:
    def __init__(
        self,
        db: Database,
        ledger: LedgerService,
        parties: PartyService,
        invoices: InvoiceService,
        settings: SettingsService,
        audit: AuditService | None = None,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.parties = parties
        self.invoices = invoices
        self.settings = settings
        self.audit = audit or AuditService(db)

    def record(
        self,
        party_id: str,
        day: str | date,
        amount: Money,
        bank_account_id: str,
        *,
        kind: str,
        allocations: Iterable[PaymentAllocation] = (),
        reference: str = "",
        actor: str = "system",
    ) -> Payment:
        if kind not in {"receipt", "payment"}:
            raise ValidationError("payment kind must be receipt or payment")
        party = self.parties.get(party_id)
        if party.currency != amount.currency:
            raise CurrencyMismatchError("payment currency differs from party currency")
        if amount.amount <= 0:
            raise ValidationError("payment amount must be positive")
        self.ledger.accounts.get(bank_account_id)
        settings = self.settings.get()
        control_id = (
            party.receivable_account_id or settings.accounts_receivable_id
            if kind == "receipt"
            else party.payable_account_id or settings.accounts_payable_id
        )
        if not control_id:
            raise ValidationError("party control account is not configured")

        allocation_list = list(allocations)
        allocated = Money.zero(amount.currency)
        for allocation in allocation_list:
            invoice = self.invoices.get(allocation.invoice_id)
            if invoice.party_id != party.id:
                raise ValidationError("payment allocation references another party")
            if invoice.currency != amount.currency:
                raise CurrencyMismatchError("payment allocation currency differs")
            if invoice.status not in {
                InvoiceStatus.ISSUED,
                InvoiceStatus.PARTIALLY_PAID,
            }:
                raise ConflictError(
                    f"cannot allocate to invoice in status {invoice.status.value}"
                )
            if allocation.amount.amount <= 0:
                raise ValidationError("allocation amount must be positive")
            if allocation.amount > self.invoices.outstanding(invoice.id):
                raise ValidationError(
                    f"allocation exceeds outstanding balance of {invoice.number}"
                )
            allocated += allocation.amount
        if allocated > amount:
            raise ValidationError("payment allocations exceed payment amount")

        payment = Payment(
            id=new_id("pay"),
            party_id=party.id,
            date=parse_date(day),
            amount=amount,
            account_id=bank_account_id,
            kind=kind,
            reference=reference,
            allocations=allocation_list,
        )

        if kind == "receipt":
            lines = [
                JournalLine(
                    account_id=bank_account_id,
                    debit=amount,
                    credit=Money.zero(amount.currency),
                    memo=reference,
                    party_id=party.id,
                ),
                JournalLine(
                    account_id=control_id,
                    debit=Money.zero(amount.currency),
                    credit=amount,
                    memo=reference,
                    party_id=party.id,
                ),
            ]
        else:
            lines = [
                JournalLine(
                    account_id=control_id,
                    debit=amount,
                    credit=Money.zero(amount.currency),
                    memo=reference,
                    party_id=party.id,
                ),
                JournalLine(
                    account_id=bank_account_id,
                    debit=Money.zero(amount.currency),
                    credit=amount,
                    memo=reference,
                    party_id=party.id,
                ),
            ]

        with self.db.transaction():
            entry = self.ledger.create_and_post(
                payment.date,
                f"{'Receipt from' if kind == 'receipt' else 'Payment to'} {party.name}",
                amount.currency,
                lines,
                reference=reference,
                source="payment",
                source_id=payment.id,
                actor=actor,
            )
            payment.journal_entry_id = entry.id
            self.db.execute(
                """
                INSERT INTO payments(
                    id, party_id, payment_date, amount_minor, currency, account_id,
                    kind, reference, journal_entry_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payment.id,
                    payment.party_id,
                    payment.date.isoformat(),
                    payment.amount.minor_units,
                    payment.amount.currency,
                    payment.account_id,
                    payment.kind,
                    payment.reference,
                    payment.journal_entry_id,
                ),
            )
            self.db.executemany(
                """
                INSERT INTO payment_allocations(
                    payment_id, invoice_id, amount_minor, currency
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        payment.id,
                        allocation.invoice_id,
                        allocation.amount.minor_units,
                        allocation.amount.currency,
                    )
                    for allocation in payment.allocations
                ],
            )
            for allocation in payment.allocations:
                self.invoices.refresh_status(allocation.invoice_id)
            self.audit.record(actor, "payment.record", payment.id, self.to_dict(payment))
        return payment

    def auto_allocate(
        self,
        party_id: str,
        amount: Money,
        *,
        kind: str,
        as_of: str | date | None = None,
    ) -> list[PaymentAllocation]:
        party = self.parties.get(party_id)
        if amount.currency != party.currency:
            raise CurrencyMismatchError("allocation currency differs from party")
        invoices = [
            invoice
            for invoice in self.invoices.list(party_id=party.id, kind="sale" if kind == "receipt" else "purchase")
            if invoice.status in {InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID}
        ]
        if as_of is not None:
            cutoff = parse_date(as_of)
            invoices = [invoice for invoice in invoices if invoice.issue_date <= cutoff]
        invoices.sort(key=lambda invoice: (invoice.due_date, invoice.issue_date, invoice.number))
        remaining = amount
        result: list[PaymentAllocation] = []
        for invoice in invoices:
            if remaining.amount <= 0:
                break
            outstanding = self.invoices.outstanding(invoice.id)
            use = outstanding if outstanding <= remaining else remaining
            if use.amount > 0:
                result.append(PaymentAllocation(invoice_id=invoice.id, amount=use))
                remaining -= use
        return result

    def get(self, payment_id: str) -> Payment:
        row = self.db.one("SELECT * FROM payments WHERE id=?", (payment_id,))
        if row is None:
            raise NotFoundError("payment", payment_id)
        allocations = [
            PaymentAllocation(
                invoice_id=str(item["invoice_id"]),
                amount=Money.from_minor(int(item["amount_minor"]), str(item["currency"])),
            )
            for item in self.db.all(
                "SELECT * FROM payment_allocations WHERE payment_id=? ORDER BY invoice_id",
                (payment_id,),
            )
        ]
        return Payment(
            id=str(row["id"]),
            party_id=str(row["party_id"]),
            date=parse_date(str(row["payment_date"])),
            amount=Money.from_minor(int(row["amount_minor"]), str(row["currency"])),
            account_id=str(row["account_id"]),
            kind=str(row["kind"]),
            reference=str(row["reference"]),
            allocations=allocations,
            journal_entry_id=None
            if row["journal_entry_id"] is None
            else str(row["journal_entry_id"]),
        )

    def unallocated(self, payment_id: str) -> Money:
        payment = self.get(payment_id)
        total = Money.zero(payment.amount.currency)
        for allocation in payment.allocations:
            total += allocation.amount
        return payment.amount - total

    def list(self, *, party_id: str | None = None, limit: int = 1000) -> list[Payment]:
        params: tuple[Any, ...]
        if party_id:
            sql = """
                SELECT id FROM payments
                WHERE party_id=?
                ORDER BY payment_date DESC, id DESC LIMIT ?
            """
            params = (party_id, max(1, min(limit, 5000)))
        else:
            sql = "SELECT id FROM payments ORDER BY payment_date DESC, id DESC LIMIT ?"
            params = (max(1, min(limit, 5000)),)
        return [self.get(str(row["id"])) for row in self.db.all(sql, params)]

    @staticmethod
    def to_dict(payment: Payment) -> dict[str, Any]:
        return {
            "id": payment.id,
            "party_id": payment.party_id,
            "date": payment.date.isoformat(),
            "amount": payment.amount.to_dict(),
            "account_id": payment.account_id,
            "kind": payment.kind,
            "reference": payment.reference,
            "journal_entry_id": payment.journal_entry_id,
            "allocations": [
                {"invoice_id": item.invoice_id, "amount": item.amount.to_dict()}
                for item in payment.allocations
            ],
        }
