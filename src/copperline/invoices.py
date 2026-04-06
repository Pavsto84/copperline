"""Sales/purchase invoices, taxes, credit notes, and posting."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable

from .accounts import AccountService
from .audit import AuditService
from .db import Database
from .errors import ConflictError, CurrencyMismatchError, NotFoundError, ValidationError
from .ids import new_id
from .jsonutil import dumps, loads
from .ledger import LedgerService
from .models import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    JournalLine,
    PartyKind,
)
from .money import Money
from .parties import PartyService
from .settings import SettingsService
from .tax import TaxBreakdown, TaxService
from .timeutil import parse_date


class InvoiceService:
    def __init__(
        self,
        db: Database,
        ledger: LedgerService,
        parties: PartyService,
        taxes: TaxService,
        settings: SettingsService,
        audit: AuditService | None = None,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.parties = parties
        self.taxes = taxes
        self.settings = settings
        self.audit = audit or AuditService(db)
        self.accounts = ledger.accounts

    def create(
        self,
        party_id: str,
        issue_date: str | date,
        lines: Iterable[InvoiceLine],
        *,
        kind: str = "sale",
        number: str | None = None,
        due_date: str | date | None = None,
        notes: str = "",
        credit_of_invoice_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> Invoice:
        party = self.parties.get(party_id)
        if kind not in {"sale", "purchase"}:
            raise ValidationError("invoice kind must be sale or purchase")
        if kind == "sale" and party.kind not in {PartyKind.CUSTOMER, PartyKind.BOTH}:
            raise ValidationError("sales invoice party is not a customer")
        if kind == "purchase" and party.kind not in {PartyKind.VENDOR, PartyKind.BOTH}:
            raise ValidationError("purchase invoice party is not a vendor")
        issue = parse_date(issue_date)
        due = (
            parse_date(due_date)
            if due_date is not None
            else issue + timedelta(days=party.payment_terms_days)
        )
        if due < issue:
            raise ValidationError("invoice due date precedes issue date")
        line_list = list(lines)
        if not line_list:
            raise ValidationError("invoice requires at least one line")
        for line in line_list:
            if line.unit_price.currency != party.currency:
                raise CurrencyMismatchError(
                    f"invoice line is {line.unit_price.currency}, party is {party.currency}"
                )
            self.accounts.get(line.account_id)
            if line.tax_code:
                self.taxes.get(line.tax_code)
        invoice_id = new_id("inv")
        assigned_number = number or self._next_number(kind, issue)
        invoice = Invoice(
            id=invoice_id,
            number=assigned_number,
            party_id=party.id,
            issue_date=issue,
            due_date=due,
            currency=party.currency,
            lines=line_list,
            kind=kind,
            notes=notes,
            credit_of_invoice_id=credit_of_invoice_id,
            metadata=metadata or {},
        )
        with self.db.transaction():
            self._insert(invoice)
            self.audit.record(actor, "invoice.create", invoice.id, self.to_dict(invoice))
        return invoice

    def _next_number(self, kind: str, day: date) -> str:
        prefix = "INV" if kind == "sale" else "BILL"
        key = f"sequence:{prefix}:{day.year}"
        with self.db.transaction():
            value = int(self.db.get_setting(key, 0)) + 1
            self.db.set_setting(key, value)
        return f"{prefix}-{day.year}-{value:05d}"

    def _insert(self, invoice: Invoice) -> None:
        try:
            self.db.execute(
                """
                INSERT INTO invoices(
                    id, number, party_id, issue_date, due_date, currency, kind,
                    status, notes, journal_entry_id, credit_of_invoice_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice.id,
                    invoice.number,
                    invoice.party_id,
                    invoice.issue_date.isoformat(),
                    invoice.due_date.isoformat(),
                    invoice.currency,
                    invoice.kind,
                    invoice.status.value,
                    invoice.notes,
                    invoice.journal_entry_id,
                    invoice.credit_of_invoice_id,
                    dumps(invoice.metadata),
                ),
            )
        except Exception as exc:
            if "UNIQUE constraint failed: invoices.number" in str(exc):
                raise ConflictError(f"invoice number already exists: {invoice.number}") from exc
            raise
        rows: list[tuple[Any, ...]] = []
        for index, line in enumerate(invoice.lines, start=1):
            rows.append(
                (
                    invoice.id,
                    index,
                    line.description,
                    format(line.quantity, "f"),
                    line.unit_price.minor_units,
                    line.unit_price.currency,
                    line.account_id,
                    line.item_id,
                    line.tax_code,
                    format(line.discount_percent, "f"),
                    dumps(line.dimensions),
                )
            )
        self.db.executemany(
            """
            INSERT INTO invoice_lines(
                invoice_id, line_no, description, quantity, unit_price_minor, currency,
                account_id, item_id, tax_code, discount_percent, dimensions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def get(self, invoice_id_or_number: str) -> Invoice:
        row = self.db.one(
            "SELECT * FROM invoices WHERE id=? OR number=?",
            (invoice_id_or_number, invoice_id_or_number),
        )
        if row is None:
            raise NotFoundError("invoice", invoice_id_or_number)
        line_rows = self.db.all(
            "SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY line_no",
            (row["id"],),
        )
        lines = [
            InvoiceLine(
                description=str(line["description"]),
                quantity=Decimal(str(line["quantity"])),
                unit_price=Money.from_minor(int(line["unit_price_minor"]), str(line["currency"])),
                account_id=str(line["account_id"]),
                item_id=None if line["item_id"] is None else str(line["item_id"]),
                tax_code=None if line["tax_code"] is None else str(line["tax_code"]),
                discount_percent=Decimal(str(line["discount_percent"])),
                dimensions=loads(str(line["dimensions_json"])),
            )
            for line in line_rows
        ]
        return Invoice(
            id=str(row["id"]),
            number=str(row["number"]),
            party_id=str(row["party_id"]),
            issue_date=parse_date(str(row["issue_date"])),
            due_date=parse_date(str(row["due_date"])),
            currency=str(row["currency"]),
            lines=lines,
            kind=str(row["kind"]),
            status=InvoiceStatus(str(row["status"])),
            notes=str(row["notes"]),
            journal_entry_id=None
            if row["journal_entry_id"] is None
            else str(row["journal_entry_id"]),
            credit_of_invoice_id=None
            if row["credit_of_invoice_id"] is None
            else str(row["credit_of_invoice_id"]),
            metadata=loads(str(row["metadata_json"])),
        )

    def list(
        self,
        *,
        party_id: str | None = None,
        status: InvoiceStatus | str | None = None,
        kind: str | None = None,
        due_before: str | date | None = None,
        limit: int = 1000,
    ) -> list[Invoice]:
        clauses = ["1=1"]
        params: list[Any] = []
        if party_id:
            clauses.append("party_id=?")
            params.append(party_id)
        if status:
            clauses.append("status=?")
            params.append(InvoiceStatus(status).value)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if due_before:
            clauses.append("due_date<=?")
            params.append(parse_date(due_before).isoformat())
        params.append(max(1, min(limit, 5000)))
        rows = self.db.all(
            f"""
            SELECT id FROM invoices
            WHERE {' AND '.join(clauses)}
            ORDER BY issue_date DESC, number DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [self.get(str(row["id"])) for row in rows]

    def line_breakdowns(self, invoice: Invoice) -> list[TaxBreakdown]:
        return [self.taxes.calculate_line(line) for line in invoice.lines]

    def totals(self, invoice: Invoice | str) -> dict[str, Money]:
        value = self.get(invoice) if isinstance(invoice, str) else invoice
        breakdowns = self.line_breakdowns(value)
        currency = value.currency
        net = Money.zero(currency)
        tax = Money.zero(currency)
        gross = Money.zero(currency)
        for result in breakdowns:
            net += result.net
            tax += result.tax
            gross += result.gross
        return {"net": net, "tax": tax, "gross": gross}

    def issue(self, invoice_id: str, *, actor: str = "system") -> Invoice:
        with self.db.transaction():
            invoice = self.get(invoice_id)
            if invoice.status == InvoiceStatus.ISSUED:
                return invoice
            if invoice.status != InvoiceStatus.DRAFT:
                raise ConflictError(f"cannot issue invoice in status {invoice.status.value}")
            party = self.parties.get(invoice.party_id)
            settings = self.settings.get()
            if invoice.kind == "sale":
                control_id = party.receivable_account_id or settings.accounts_receivable_id
                if not control_id:
                    raise ValidationError("accounts receivable account is not configured")
            else:
                control_id = party.payable_account_id or settings.accounts_payable_id
                if not control_id:
                    raise ValidationError("accounts payable account is not configured")
            self.ledger.periods.ensure_open(invoice.issue_date)
            journal_lines = self._journal_lines(invoice, control_id)
            entry = self.ledger.create_and_post(
                invoice.issue_date,
                f"{'Invoice' if invoice.kind == 'sale' else 'Bill'} {invoice.number}",
                invoice.currency,
                journal_lines,
                reference=invoice.number,
                source="invoice",
                source_id=invoice.id,
                actor=actor,
            )
            self.db.execute(
                """
                UPDATE invoices
                SET status='issued', journal_entry_id=?
                WHERE id=?
                """,
                (entry.id, invoice.id),
            )
            invoice.status = InvoiceStatus.ISSUED
            invoice.journal_entry_id = entry.id
            self.audit.record(actor, "invoice.issue", invoice.id, {"journal_entry_id": entry.id})
            return invoice

    def _journal_lines(self, invoice: Invoice, control_id: str) -> list[JournalLine]:
        totals = self.totals(invoice)
        result: list[JournalLine] = []

        def add_signed(
            account_id: str,
            amount: Money,
            *,
            positive_side: str,
            memo: str,
            party_id: str | None = None,
            item_id: str | None = None,
            tax_code: str | None = None,
            dimensions: dict[str, str] | None = None,
        ) -> None:
            value = abs(amount)
            side = positive_side if amount.amount >= 0 else (
                "credit" if positive_side == "debit" else "debit"
            )
            result.append(
                JournalLine(
                    account_id=account_id,
                    debit=value if side == "debit" else Money.zero(invoice.currency),
                    credit=value if side == "credit" else Money.zero(invoice.currency),
                    memo=memo,
                    party_id=party_id,
                    item_id=item_id,
                    tax_code=tax_code,
                    dimensions=dict(dimensions or {}),
                )
            )

        if invoice.kind == "sale":
            add_signed(
                control_id,
                totals["gross"],
                positive_side="debit",
                memo=invoice.number,
                party_id=invoice.party_id,
            )
            for line, breakdown in zip(invoice.lines, self.line_breakdowns(invoice)):
                add_signed(
                    line.account_id,
                    breakdown.net,
                    positive_side="credit",
                    memo=line.description,
                    party_id=invoice.party_id,
                    item_id=line.item_id,
                    tax_code=line.tax_code,
                    dimensions=line.dimensions,
                )
                if breakdown.tax:
                    tax_rate = self.taxes.get(line.tax_code or "")
                    add_signed(
                        tax_rate.account_id,
                        breakdown.tax,
                        positive_side="credit",
                        memo=f"Tax {tax_rate.code}: {line.description}",
                        party_id=invoice.party_id,
                        tax_code=tax_rate.code,
                    )
        else:
            add_signed(
                control_id,
                totals["gross"],
                positive_side="credit",
                memo=invoice.number,
                party_id=invoice.party_id,
            )
            for line, breakdown in zip(invoice.lines, self.line_breakdowns(invoice)):
                expense_amount = breakdown.net + breakdown.nonrecoverable
                add_signed(
                    line.account_id,
                    expense_amount,
                    positive_side="debit",
                    memo=line.description,
                    party_id=invoice.party_id,
                    item_id=line.item_id,
                    tax_code=line.tax_code,
                    dimensions=line.dimensions,
                )
                if breakdown.recoverable:
                    tax_rate = self.taxes.get(line.tax_code or "")
                    add_signed(
                        tax_rate.account_id,
                        breakdown.recoverable,
                        positive_side="debit",
                        memo=f"Recoverable tax {tax_rate.code}: {line.description}",
                        party_id=invoice.party_id,
                        tax_code=tax_rate.code,
                    )
        return result

    def paid_amount(self, invoice_id: str) -> Money:
        invoice = self.get(invoice_id)
        value = int(
            self.db.scalar(
                """
                SELECT COALESCE(SUM(amount_minor), 0)
                FROM payment_allocations
                WHERE invoice_id=?
                """,
                (invoice.id,),
                default=0,
            )
        )
        return Money.from_minor(value, invoice.currency)

    def outstanding(self, invoice_id: str) -> Money:
        invoice = self.get(invoice_id)
        return self.totals(invoice)["gross"] - self.paid_amount(invoice.id)

    def refresh_status(self, invoice_id: str) -> Invoice:
        invoice = self.get(invoice_id)
        if invoice.status in {InvoiceStatus.DRAFT, InvoiceStatus.VOID}:
            return invoice
        outstanding = self.outstanding(invoice.id)
        paid = self.paid_amount(invoice.id)
        if outstanding.amount <= 0:
            status = InvoiceStatus.PAID
        elif paid.amount > 0:
            status = InvoiceStatus.PARTIALLY_PAID
        else:
            status = InvoiceStatus.ISSUED
        self.db.execute("UPDATE invoices SET status=? WHERE id=?", (status.value, invoice.id))
        invoice.status = status
        return invoice

    def credit_note(
        self,
        invoice_id: str,
        lines: Iterable[InvoiceLine] | None = None,
        *,
        issue_date: str | date | None = None,
        notes: str = "",
        actor: str = "system",
    ) -> Invoice:
        original = self.get(invoice_id)
        if original.status == InvoiceStatus.DRAFT:
            raise ConflictError("cannot credit a draft invoice")
        source_lines = list(lines) if lines is not None else original.lines
        negative_lines = [
            InvoiceLine(
                description=f"Credit: {line.description}",
                quantity=line.quantity,
                unit_price=-line.unit_price,
                account_id=line.account_id,
                item_id=line.item_id,
                tax_code=line.tax_code,
                discount_percent=line.discount_percent,
                dimensions=dict(line.dimensions),
            )
            for line in source_lines
        ]
        note = self.create(
            original.party_id,
            issue_date or date.today(),
            negative_lines,
            kind=original.kind,
            notes=notes,
            credit_of_invoice_id=original.id,
            actor=actor,
        )
        return self.issue(note.id, actor=actor)

    def void_draft(self, invoice_id: str, *, actor: str = "system") -> Invoice:
        invoice = self.get(invoice_id)
        if invoice.status != InvoiceStatus.DRAFT:
            raise ConflictError("only draft invoices can be voided directly")
        self.db.execute("UPDATE invoices SET status='void' WHERE id=?", (invoice.id,))
        invoice.status = InvoiceStatus.VOID
        self.audit.record(actor, "invoice.void", invoice.id)
        return invoice

    def to_dict(self, invoice: Invoice) -> dict[str, Any]:
        totals = self.totals(invoice)
        return {
            "id": invoice.id,
            "number": invoice.number,
            "party_id": invoice.party_id,
            "issue_date": invoice.issue_date.isoformat(),
            "due_date": invoice.due_date.isoformat(),
            "currency": invoice.currency,
            "kind": invoice.kind,
            "status": invoice.status.value,
            "notes": invoice.notes,
            "journal_entry_id": invoice.journal_entry_id,
            "credit_of_invoice_id": invoice.credit_of_invoice_id,
            "metadata": invoice.metadata,
            "lines": [
                {
                    "description": line.description,
                    "quantity": format(line.quantity, "f"),
                    "unit_price": line.unit_price.to_dict(),
                    "account_id": line.account_id,
                    "item_id": line.item_id,
                    "tax_code": line.tax_code,
                    "discount_percent": format(line.discount_percent, "f"),
                    "dimensions": line.dimensions,
                }
                for line in invoice.lines
            ],
            "totals": {name: value.to_dict() for name, value in totals.items()},
        }
