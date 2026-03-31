"""Double-entry journal posting, balances, reversals, and idempotency."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Iterable

from .accounts import AccountService, PeriodService
from .audit import AuditService
from .db import Database
from .errors import (
    ConflictError,
    CurrencyMismatchError,
    NotFoundError,
    UnbalancedEntryError,
    ValidationError,
)
from .ids import new_id
from .jsonutil import dumps, json_hash, loads
from .models import AccountType, JournalEntry, JournalLine, JournalStatus
from .money import Money, sum_money
from .timeutil import parse_date


class LedgerService:
    def __init__(
        self,
        db: Database,
        accounts: AccountService | None = None,
        periods: PeriodService | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self.db = db
        self.audit = audit or AuditService(db)
        self.accounts = accounts or AccountService(db, self.audit)
        self.periods = periods or PeriodService(db, self.audit)

    def draft(
        self,
        entry_date: str | date,
        description: str,
        currency: str,
        lines: Iterable[JournalLine],
        *,
        reference: str = "",
        source: str = "manual",
        source_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> JournalEntry:
        line_list = list(lines)
        entry = JournalEntry(
            id=new_id("je"),
            date=parse_date(entry_date),
            description=description.strip(),
            currency=currency,
            lines=line_list,
            reference=reference,
            source=source,
            source_id=source_id,
            metadata=metadata or {},
        )
        self.validate(entry)
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO journal_entries(
                    id, entry_date, description, currency, status, reference,
                    source, source_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.date.isoformat(),
                    entry.description,
                    entry.currency,
                    entry.status.value,
                    entry.reference,
                    entry.source,
                    entry.source_id,
                    dumps(entry.metadata),
                ),
            )
            self._insert_lines(entry)
            self.audit.record(actor, "journal.draft", entry.id, self.entry_dict(entry))
        return entry

    def post(
        self,
        entry_or_id: JournalEntry | str,
        *,
        actor: str = "system",
    ) -> JournalEntry:
        with self.db.transaction():
            entry = entry_or_id if isinstance(entry_or_id, JournalEntry) else self.get(entry_or_id)
            if entry.status == JournalStatus.POSTED:
                return entry
            if entry.status == JournalStatus.VOID:
                raise ConflictError("void journal entry cannot be posted")
            self.periods.ensure_open(entry.date)
            self.validate(entry)
            posted_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            self.db.execute(
                "UPDATE journal_entries SET status='posted', posted_at=? WHERE id=?",
                (posted_at, entry.id),
            )
            entry.status = JournalStatus.POSTED
            entry.posted_at = posted_at
            self.audit.record(actor, "journal.post", entry.id, self.entry_dict(entry))
            return entry

    def create_and_post(
        self,
        entry_date: str | date,
        description: str,
        currency: str,
        lines: Iterable[JournalLine],
        **kwargs: Any,
    ) -> JournalEntry:
        with self.db.transaction():
            entry = self.draft(entry_date, description, currency, lines, **kwargs)
            return self.post(entry, actor=str(kwargs.get("actor", "system")))

    def validate(self, entry: JournalEntry) -> None:
        if not entry.description:
            raise ValidationError("journal description may not be empty")
        if len(entry.lines) < 2:
            raise ValidationError("journal entry requires at least two lines")
        debit = Money.zero(entry.currency)
        credit = Money.zero(entry.currency)
        for index, line in enumerate(entry.lines, start=1):
            if line.debit.currency != entry.currency or line.credit.currency != entry.currency:
                raise CurrencyMismatchError(
                    f"journal line {index} currency differs from entry currency"
                )
            account = self.accounts.get(line.account_id)
            if not account.active:
                raise ValidationError(f"journal line {index} uses inactive account {account.code}")
            if account.currency != entry.currency:
                raise CurrencyMismatchError(
                    f"account {account.code} is {account.currency}, entry is {entry.currency}"
                )
            if line.debit.is_zero() and line.credit.is_zero():
                raise ValidationError(f"journal line {index} has zero debit and credit")
            debit += line.debit
            credit += line.credit
        if debit != credit:
            raise UnbalancedEntryError(
                f"journal entry is unbalanced by {(debit - credit).format()}",
                details={
                    "debit": debit.to_dict(),
                    "credit": credit.to_dict(),
                    "difference": (debit - credit).to_dict(),
                },
            )

    def _insert_lines(self, entry: JournalEntry) -> None:
        rows: list[tuple[Any, ...]] = []
        for index, line in enumerate(entry.lines, start=1):
            rows.append(
                (
                    entry.id,
                    index,
                    line.account_id,
                    line.debit.minor_units,
                    line.credit.minor_units,
                    entry.currency,
                    line.memo,
                    line.party_id,
                    line.item_id,
                    line.tax_code,
                    dumps(line.dimensions),
                )
            )
        self.db.executemany(
            """
            INSERT INTO journal_lines(
                entry_id, line_no, account_id, debit_minor, credit_minor,
                currency, memo, party_id, item_id, tax_code, dimensions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def get(self, entry_id: str) -> JournalEntry:
        row = self.db.one("SELECT * FROM journal_entries WHERE id=?", (entry_id,))
        if row is None:
            raise NotFoundError("journal entry", entry_id)
        line_rows = self.db.all(
            "SELECT * FROM journal_lines WHERE entry_id=? ORDER BY line_no",
            (entry_id,),
        )
        lines = [
            JournalLine(
                account_id=str(line["account_id"]),
                debit=Money.from_minor(int(line["debit_minor"]), str(line["currency"])),
                credit=Money.from_minor(int(line["credit_minor"]), str(line["currency"])),
                memo=str(line["memo"]),
                party_id=None if line["party_id"] is None else str(line["party_id"]),
                item_id=None if line["item_id"] is None else str(line["item_id"]),
                tax_code=None if line["tax_code"] is None else str(line["tax_code"]),
                dimensions=loads(str(line["dimensions_json"])),
            )
            for line in line_rows
        ]
        return JournalEntry(
            id=str(row["id"]),
            date=parse_date(str(row["entry_date"])),
            description=str(row["description"]),
            currency=str(row["currency"]),
            lines=lines,
            status=JournalStatus(str(row["status"])),
            reference=str(row["reference"]),
            source=str(row["source"]),
            source_id=None if row["source_id"] is None else str(row["source_id"]),
            posted_at=None if row["posted_at"] is None else str(row["posted_at"]),
            voided_by=None if row["voided_by"] is None else str(row["voided_by"]),
            metadata=loads(str(row["metadata_json"])),
        )

    def list(
        self,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
        status: JournalStatus | str | None = None,
        source: str | None = None,
        account_id: str | None = None,
        limit: int = 500,
    ) -> list[JournalEntry]:
        clauses = ["1=1"]
        params: list[Any] = []
        if start is not None:
            clauses.append("j.entry_date >= ?")
            params.append(parse_date(start).isoformat())
        if end is not None:
            clauses.append("j.entry_date <= ?")
            params.append(parse_date(end).isoformat())
        if status is not None:
            clauses.append("j.status = ?")
            params.append(JournalStatus(status).value)
        if source is not None:
            clauses.append("j.source = ?")
            params.append(source)
        join = ""
        if account_id is not None:
            join = "JOIN journal_lines l ON l.entry_id=j.id"
            clauses.append("l.account_id = ?")
            params.append(account_id)
        params.append(max(1, min(limit, 5000)))
        ids = [
            str(row["id"])
            for row in self.db.all(
                f"""
                SELECT DISTINCT j.id, j.entry_date
                FROM journal_entries j {join}
                WHERE {' AND '.join(clauses)}
                ORDER BY j.entry_date DESC, j.id DESC
                LIMIT ?
                """,
                tuple(params),
            )
        ]
        return [self.get(identifier) for identifier in ids]

    def balance(
        self,
        account_id: str,
        *,
        as_of: str | date | None = None,
        dimensions: dict[str, str] | None = None,
    ) -> Money:
        account = self.accounts.get(account_id)
        clauses = ["l.account_id = ?", "j.status = 'posted'"]
        params: list[Any] = [account.id]
        if as_of is not None:
            clauses.append("j.entry_date <= ?")
            params.append(parse_date(as_of).isoformat())
        rows = self.db.all(
            f"""
            SELECT l.debit_minor, l.credit_minor, l.dimensions_json
            FROM journal_lines l
            JOIN journal_entries j ON j.id=l.entry_id
            WHERE {' AND '.join(clauses)}
            """,
            tuple(params),
        )
        debit = 0
        credit = 0
        for row in rows:
            if dimensions:
                actual = loads(str(row["dimensions_json"]))
                if any(actual.get(key) != value for key, value in dimensions.items()):
                    continue
            debit += int(row["debit_minor"])
            credit += int(row["credit_minor"])
        net = debit - credit
        if account.normal_balance.value == "credit":
            net = -net
        return Money.from_minor(net, account.currency)

    def raw_debit_credit(
        self,
        account_id: str,
        *,
        start: str | date | None = None,
        end: str | date | None = None,
    ) -> tuple[Money, Money]:
        account = self.accounts.get(account_id)
        clauses = ["l.account_id = ?", "j.status = 'posted'"]
        params: list[Any] = [account.id]
        if start is not None:
            clauses.append("j.entry_date >= ?")
            params.append(parse_date(start).isoformat())
        if end is not None:
            clauses.append("j.entry_date <= ?")
            params.append(parse_date(end).isoformat())
        row = self.db.one(
            f"""
            SELECT COALESCE(SUM(l.debit_minor),0) AS debit,
                   COALESCE(SUM(l.credit_minor),0) AS credit
            FROM journal_lines l
            JOIN journal_entries j ON j.id=l.entry_id
            WHERE {' AND '.join(clauses)}
            """,
            tuple(params),
        )
        assert row is not None
        return (
            Money.from_minor(int(row["debit"]), account.currency),
            Money.from_minor(int(row["credit"]), account.currency),
        )

    def reverse(
        self,
        entry_id: str,
        reversal_date: str | date,
        *,
        reason: str,
        actor: str = "system",
    ) -> JournalEntry:
        original = self.get(entry_id)
        if original.status != JournalStatus.POSTED:
            raise ConflictError("only posted entries can be reversed")
        if not reason.strip():
            raise ValidationError("reversal reason is required")
        lines = [
            JournalLine(
                account_id=line.account_id,
                debit=line.credit,
                credit=line.debit,
                memo=f"Reversal: {line.memo}".strip(),
                party_id=line.party_id,
                item_id=line.item_id,
                tax_code=line.tax_code,
                dimensions=dict(line.dimensions),
            )
            for line in original.lines
        ]
        reversal = self.create_and_post(
            reversal_date,
            f"Reversal of {original.id}: {reason}",
            original.currency,
            lines,
            reference=original.reference,
            source="reversal",
            source_id=original.id,
            metadata={"reversal_reason": reason},
            actor=actor,
        )
        return reversal

    def void_draft(self, entry_id: str, *, actor: str = "system") -> JournalEntry:
        entry = self.get(entry_id)
        if entry.status == JournalStatus.POSTED:
            raise ConflictError("posted entry must be reversed, not voided")
        if entry.status == JournalStatus.VOID:
            return entry
        self.db.execute(
            "UPDATE journal_entries SET status='void', voided_by=? WHERE id=?",
            (actor, entry.id),
        )
        entry.status = JournalStatus.VOID
        entry.voided_by = actor
        self.audit.record(actor, "journal.void", entry.id)
        return entry

    def idempotent(
        self,
        namespace: str,
        key: str,
        payload: Any,
        operation: callable,
    ) -> Any:
        payload_hash = json_hash(payload)
        with self.db.transaction():
            row = self.db.one(
                """
                SELECT payload_hash, result_json
                FROM idempotency_keys
                WHERE namespace=? AND key=?
                """,
                (namespace, key),
            )
            if row is not None:
                if str(row["payload_hash"]) != payload_hash:
                    raise ConflictError(
                        f"idempotency key reused with different payload: {key}"
                    )
                return loads(str(row["result_json"]))
            result = operation()
            self.db.execute(
                """
                INSERT INTO idempotency_keys(namespace, key, payload_hash, result_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    namespace,
                    key,
                    payload_hash,
                    dumps(result),
                    datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                ),
            )
            return result

    def trial_balance(
        self,
        *,
        as_of: str | date | None = None,
        include_zero: bool = False,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for account in self.accounts.list(active=None):
            debit, credit = self.raw_debit_credit(
                account.id,
                end=as_of,
            )
            if not include_zero and debit.is_zero() and credit.is_zero():
                continue
            result.append(
                {
                    "account": AccountService.to_dict(account),
                    "debit": debit.to_dict(),
                    "credit": credit.to_dict(),
                    "net": self.balance(account.id, as_of=as_of).to_dict(),
                }
            )
        return result

    @staticmethod
    def entry_dict(entry: JournalEntry) -> dict[str, Any]:
        return {
            "id": entry.id,
            "date": entry.date.isoformat(),
            "description": entry.description,
            "currency": entry.currency,
            "status": entry.status.value,
            "reference": entry.reference,
            "source": entry.source,
            "source_id": entry.source_id,
            "posted_at": entry.posted_at,
            "voided_by": entry.voided_by,
            "metadata": entry.metadata,
            "lines": [
                {
                    "account_id": line.account_id,
                    "debit": line.debit.to_dict(),
                    "credit": line.credit.to_dict(),
                    "memo": line.memo,
                    "party_id": line.party_id,
                    "item_id": line.item_id,
                    "tax_code": line.tax_code,
                    "dimensions": line.dimensions,
                }
                for line in entry.lines
            ],
        }
