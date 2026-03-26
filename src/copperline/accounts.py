"""Chart-of-accounts management and period locking."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Iterable

from .audit import AuditService
from .db import Database
from .errors import ConflictError, NotFoundError, PeriodLockedError, ValidationError
from .ids import new_id
from .models import Account, AccountType
from .money import normalize_currency
from .timeutil import DateRange, parse_date


class AccountService:
    def __init__(self, db: Database, audit: AuditService | None = None) -> None:
        self.db = db
        self.audit = audit or AuditService(db)

    def create(
        self,
        code: str,
        name: str,
        type: AccountType | str,
        currency: str,
        *,
        parent_id: str | None = None,
        description: str = "",
        actor: str = "system",
    ) -> Account:
        code = code.strip()
        name = name.strip()
        if not code:
            raise ValidationError("account code may not be empty")
        if not name:
            raise ValidationError("account name may not be empty")
        account_type = AccountType(type)
        currency = normalize_currency(currency)
        if parent_id is not None:
            parent = self.get(parent_id)
            if parent.type != account_type:
                raise ValidationError("child account type must match parent account type")
            if parent.currency != currency:
                raise ValidationError("child account currency must match parent")
        account = Account(
            id=new_id("acc"),
            code=code,
            name=name,
            type=account_type,
            currency=currency,
            parent_id=parent_id,
            description=description,
        )
        try:
            self.db.execute(
                """
                INSERT INTO accounts(id, code, name, type, currency, active, parent_id, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account.id,
                    account.code,
                    account.name,
                    account.type.value,
                    account.currency,
                    1,
                    account.parent_id,
                    account.description,
                ),
            )
        except Exception as exc:
            if "UNIQUE constraint failed: accounts.code" in str(exc):
                raise ConflictError(f"account code already exists: {code}") from exc
            raise
        self.audit.record(actor, "account.create", account.id, self.to_dict(account))
        return account

    def get(self, identifier: str) -> Account:
        row = self.db.one(
            """
            SELECT id, code, name, type, currency, active, parent_id, description
            FROM accounts
            WHERE id = ? OR code = ?
            """,
            (identifier, identifier),
        )
        if row is None:
            raise NotFoundError("account", identifier)
        return Account(
            id=str(row["id"]),
            code=str(row["code"]),
            name=str(row["name"]),
            type=AccountType(str(row["type"])),
            currency=str(row["currency"]),
            active=bool(row["active"]),
            parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
            description=str(row["description"]),
        )

    def list(
        self,
        *,
        type: AccountType | str | None = None,
        active: bool | None = True,
    ) -> list[Account]:
        clauses = ["1=1"]
        params: list[object] = []
        if type is not None:
            clauses.append("type = ?")
            params.append(AccountType(type).value)
        if active is not None:
            clauses.append("active = ?")
            params.append(1 if active else 0)
        rows = self.db.all(
            f"""
            SELECT id, code, name, type, currency, active, parent_id, description
            FROM accounts
            WHERE {' AND '.join(clauses)}
            ORDER BY code
            """,
            tuple(params),
        )
        return [
            Account(
                id=str(row["id"]),
                code=str(row["code"]),
                name=str(row["name"]),
                type=AccountType(str(row["type"])),
                currency=str(row["currency"]),
                active=bool(row["active"]),
                parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
                description=str(row["description"]),
            )
            for row in rows
        ]

    def deactivate(self, identifier: str, *, actor: str = "system") -> Account:
        account = self.get(identifier)
        children = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM accounts WHERE parent_id = ? AND active = 1",
                (account.id,),
                default=0,
            )
        )
        if children:
            raise ConflictError("cannot deactivate account with active child accounts")
        self.db.execute("UPDATE accounts SET active = 0 WHERE id = ?", (account.id,))
        account.active = False
        self.audit.record(actor, "account.deactivate", account.id)
        return account

    def descendants(self, identifier: str) -> list[Account]:
        root = self.get(identifier)
        rows = self.db.all(
            """
            WITH RECURSIVE tree(id) AS (
                SELECT id FROM accounts WHERE parent_id = ?
                UNION ALL
                SELECT a.id FROM accounts a JOIN tree t ON a.parent_id = t.id
            )
            SELECT a.id, a.code, a.name, a.type, a.currency, a.active, a.parent_id, a.description
            FROM accounts a JOIN tree t ON t.id = a.id
            ORDER BY a.code
            """,
            (root.id,),
        )
        return [
            Account(
                id=str(row["id"]),
                code=str(row["code"]),
                name=str(row["name"]),
                type=AccountType(str(row["type"])),
                currency=str(row["currency"]),
                active=bool(row["active"]),
                parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
                description=str(row["description"]),
            )
            for row in rows
        ]

    @staticmethod
    def to_dict(account: Account) -> dict[str, object]:
        return {
            "id": account.id,
            "code": account.code,
            "name": account.name,
            "type": account.type.value,
            "currency": account.currency,
            "active": account.active,
            "parent_id": account.parent_id,
            "description": account.description,
        }


class PeriodService:
    def __init__(self, db: Database, audit: AuditService | None = None) -> None:
        self.db = db
        self.audit = audit or AuditService(db)

    def define(self, start: str | date, end: str | date) -> DateRange:
        period = DateRange(parse_date(start), parse_date(end))
        overlapping = self.db.one(
            """
            SELECT start_date, end_date
            FROM accounting_periods
            WHERE start_date <= ? AND end_date >= ?
              AND start_date != ?
            """,
            (period.end.isoformat(), period.start.isoformat(), period.start.isoformat()),
        )
        if overlapping is not None:
            raise ConflictError(
                "accounting period overlaps an existing period",
                details={
                    "existing_start": overlapping["start_date"],
                    "existing_end": overlapping["end_date"],
                },
            )
        self.db.execute(
            """
            INSERT INTO accounting_periods(start_date, end_date, locked)
            VALUES (?, ?, 0)
            ON CONFLICT(start_date) DO UPDATE SET end_date=excluded.end_date
            """,
            (period.start.isoformat(), period.end.isoformat()),
        )
        return period

    def period_for(self, day: str | date) -> dict[str, object] | None:
        value = parse_date(day).isoformat()
        row = self.db.one(
            """
            SELECT start_date, end_date, locked, locked_at, locked_by
            FROM accounting_periods
            WHERE start_date <= ? AND end_date >= ?
            ORDER BY start_date DESC LIMIT 1
            """,
            (value, value),
        )
        if row is None:
            return None
        return {
            "start": str(row["start_date"]),
            "end": str(row["end_date"]),
            "locked": bool(row["locked"]),
            "locked_at": row["locked_at"],
            "locked_by": row["locked_by"],
        }

    def ensure_open(self, day: str | date) -> None:
        period = self.period_for(day)
        if period is not None and bool(period["locked"]):
            raise PeriodLockedError(
                f"accounting period {period['start']}..{period['end']} is locked",
                details={"date": parse_date(day).isoformat(), **period},
            )

    def lock(
        self,
        start: str | date,
        *,
        actor: str = "system",
        require_posted_drafts: bool = True,
    ) -> dict[str, object]:
        start_date = parse_date(start)
        row = self.db.one(
            "SELECT start_date, end_date, locked FROM accounting_periods WHERE start_date = ?",
            (start_date.isoformat(),),
        )
        if row is None:
            raise NotFoundError("accounting period", start_date.isoformat())
        if bool(row["locked"]):
            return self.period_for(start_date) or {}
        if require_posted_drafts:
            drafts = int(
                self.db.scalar(
                    """
                    SELECT COUNT(*) FROM journal_entries
                    WHERE status = 'draft' AND entry_date BETWEEN ? AND ?
                    """,
                    (row["start_date"], row["end_date"]),
                    default=0,
                )
            )
            if drafts:
                raise ConflictError(
                    f"period has {drafts} draft journal entries",
                    details={"drafts": drafts},
                )
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.db.execute(
            """
            UPDATE accounting_periods
            SET locked=1, locked_at=?, locked_by=?
            WHERE start_date=?
            """,
            (now, actor, start_date.isoformat()),
        )
        self.audit.record(
            actor,
            "period.lock",
            start_date.isoformat(),
            {"end": str(row["end_date"])},
        )
        return self.period_for(start_date) or {}

    def unlock(
        self,
        start: str | date,
        *,
        actor: str,
        reason: str,
    ) -> dict[str, object]:
        if not reason.strip():
            raise ValidationError("unlock reason is required")
        start_date = parse_date(start)
        row = self.db.one(
            "SELECT end_date, locked FROM accounting_periods WHERE start_date=?",
            (start_date.isoformat(),),
        )
        if row is None:
            raise NotFoundError("accounting period", start_date.isoformat())
        self.db.execute(
            """
            UPDATE accounting_periods
            SET locked=0, locked_at=NULL, locked_by=NULL
            WHERE start_date=?
            """,
            (start_date.isoformat(),),
        )
        self.audit.record(
            actor,
            "period.unlock",
            start_date.isoformat(),
            {"reason": reason, "end": str(row["end_date"])},
        )
        return self.period_for(start_date) or {}

    def list(self) -> list[dict[str, object]]:
        return [
            {
                "start": str(row["start_date"]),
                "end": str(row["end_date"]),
                "locked": bool(row["locked"]),
                "locked_at": row["locked_at"],
                "locked_by": row["locked_by"],
            }
            for row in self.db.all(
                "SELECT * FROM accounting_periods ORDER BY start_date"
            )
        ]
