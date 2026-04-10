"""Bank-account registry and normalized statement transactions."""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .audit import AuditService
from .db import Database
from .errors import ConflictError, CurrencyMismatchError, NotFoundError, ValidationError
from .ids import new_id
from .jsonutil import dumps, loads
from .models import BankAccount, BankTransaction, BankTxnStatus
from .money import Money
from .timeutil import parse_date


class BankingService:
    def __init__(self, db: Database, audit: AuditService | None = None) -> None:
        self.db = db
        self.audit = audit or AuditService(db)

    def create_account(
        self,
        name: str,
        currency: str,
        ledger_account_id: str,
        *,
        institution: str = "",
        account_mask: str = "",
        actor: str = "system",
    ) -> BankAccount:
        name = name.strip()
        if not name:
            raise ValidationError("bank account name may not be empty")
        ledger = self.db.one("SELECT currency FROM accounts WHERE id=?", (ledger_account_id,))
        if ledger is None:
            raise NotFoundError("ledger account", ledger_account_id)
        if str(ledger["currency"]) != currency.upper():
            raise CurrencyMismatchError("bank and ledger account currencies differ")
        account = BankAccount(
            id=new_id("bank"),
            name=name,
            currency=currency,
            ledger_account_id=ledger_account_id,
            institution=institution.strip(),
            account_mask=account_mask.strip(),
        )
        self.db.execute(
            """
            INSERT INTO bank_accounts(
                id, name, currency, ledger_account_id, institution, account_mask
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                account.id,
                account.name,
                account.currency,
                account.ledger_account_id,
                account.institution,
                account.account_mask,
            ),
        )
        self.audit.record(actor, "bank_account.create", account.id, self.account_dict(account))
        return account

    def get_account(self, account_id: str) -> BankAccount:
        row = self.db.one("SELECT * FROM bank_accounts WHERE id=?", (account_id,))
        if row is None:
            raise NotFoundError("bank account", account_id)
        return BankAccount(
            id=str(row["id"]),
            name=str(row["name"]),
            currency=str(row["currency"]),
            ledger_account_id=str(row["ledger_account_id"]),
            institution=str(row["institution"]),
            account_mask=str(row["account_mask"]),
        )

    def list_accounts(self) -> list[BankAccount]:
        return [
            BankAccount(
                id=str(row["id"]),
                name=str(row["name"]),
                currency=str(row["currency"]),
                ledger_account_id=str(row["ledger_account_id"]),
                institution=str(row["institution"]),
                account_mask=str(row["account_mask"]),
            )
            for row in self.db.all("SELECT * FROM bank_accounts ORDER BY name, id")
        ]

    def import_transactions(
        self,
        bank_account_id: str,
        transactions: Iterable[dict[str, Any]],
        *,
        actor: str = "system",
    ) -> dict[str, Any]:
        account = self.get_account(bank_account_id)
        inserted: list[str] = []
        duplicates: list[str] = []
        with self.db.transaction():
            for raw in transactions:
                external_id = str(raw.get("external_id", "")).strip()
                if not external_id:
                    raise ValidationError("bank transaction external_id is required")
                day = parse_date(raw["posted_date"])
                amount = (
                    raw["amount"]
                    if isinstance(raw["amount"], Money)
                    else Money(raw["amount"], account.currency)
                )
                if amount.currency != account.currency:
                    raise CurrencyMismatchError("bank transaction currency mismatch")
                description = str(raw.get("description", "")).strip()
                metadata = dict(raw.get("metadata") or {})
                transaction = BankTransaction(
                    id=new_id("btx"),
                    bank_account_id=account.id,
                    posted_date=day,
                    amount=amount,
                    description=description,
                    external_id=external_id,
                    metadata=metadata,
                )
                try:
                    self.db.execute(
                        """
                        INSERT INTO bank_transactions(
                            id, bank_account_id, posted_date, amount_minor, currency,
                            description, external_id, status, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            transaction.id,
                            transaction.bank_account_id,
                            transaction.posted_date.isoformat(),
                            transaction.amount.minor_units,
                            transaction.amount.currency,
                            transaction.description,
                            transaction.external_id,
                            transaction.status.value,
                            dumps(transaction.metadata),
                        ),
                    )
                except Exception as exc:
                    if "UNIQUE constraint failed" in str(exc):
                        duplicates.append(external_id)
                        continue
                    raise
                inserted.append(transaction.id)
            self.audit.record(
                actor,
                "bank.import",
                account.id,
                {"inserted": len(inserted), "duplicates": len(duplicates)},
            )
        return {"inserted": inserted, "duplicates": duplicates}

    def get_transaction(self, transaction_id: str) -> BankTransaction:
        row = self.db.one("SELECT * FROM bank_transactions WHERE id=?", (transaction_id,))
        if row is None:
            raise NotFoundError("bank transaction", transaction_id)
        return self._row(row)

    def list_transactions(
        self,
        bank_account_id: str,
        *,
        status: BankTxnStatus | str | None = None,
        start: str | date | None = None,
        end: str | date | None = None,
        search: str | None = None,
        limit: int = 2000,
    ) -> list[BankTransaction]:
        account = self.get_account(bank_account_id)
        clauses = ["bank_account_id=?"]
        params: list[Any] = [account.id]
        if status:
            clauses.append("status=?")
            params.append(BankTxnStatus(status).value)
        if start:
            clauses.append("posted_date>=?")
            params.append(parse_date(start).isoformat())
        if end:
            clauses.append("posted_date<=?")
            params.append(parse_date(end).isoformat())
        if search:
            clauses.append("LOWER(description) LIKE ?")
            params.append(f"%{search.lower()}%")
        params.append(max(1, min(limit, 10000)))
        return [
            self._row(row)
            for row in self.db.all(
                f"""
                SELECT * FROM bank_transactions
                WHERE {' AND '.join(clauses)}
                ORDER BY posted_date, id
                LIMIT ?
                """,
                tuple(params),
            )
        ]

    def mark_ignored(
        self,
        transaction_id: str,
        *,
        reason: str,
        actor: str = "system",
    ) -> BankTransaction:
        transaction = self.get_transaction(transaction_id)
        if transaction.status == BankTxnStatus.MATCHED:
            raise ConflictError("matched bank transaction cannot be ignored")
        if not reason.strip():
            raise ValidationError("ignore reason is required")
        metadata = dict(transaction.metadata)
        metadata["ignore_reason"] = reason
        self.db.execute(
            """
            UPDATE bank_transactions
            SET status='ignored', metadata_json=?
            WHERE id=?
            """,
            (dumps(metadata), transaction.id),
        )
        transaction.status = BankTxnStatus.IGNORED
        transaction.metadata = metadata
        self.audit.record(actor, "bank.ignore", transaction.id, {"reason": reason})
        return transaction

    def unmatch(self, transaction_id: str, *, actor: str = "system") -> BankTransaction:
        transaction = self.get_transaction(transaction_id)
        if transaction.status != BankTxnStatus.MATCHED:
            return transaction
        self.db.execute(
            """
            UPDATE bank_transactions
            SET status='unmatched', matched_entry_id=NULL
            WHERE id=?
            """,
            (transaction.id,),
        )
        self.audit.record(
            actor,
            "bank.unmatch",
            transaction.id,
            {"entry_id": transaction.matched_entry_id},
        )
        transaction.status = BankTxnStatus.UNMATCHED
        transaction.matched_entry_id = None
        return transaction

    @staticmethod
    def account_dict(account: BankAccount) -> dict[str, Any]:
        return {
            "id": account.id,
            "name": account.name,
            "currency": account.currency,
            "ledger_account_id": account.ledger_account_id,
            "institution": account.institution,
            "account_mask": account.account_mask,
        }

    @staticmethod
    def transaction_dict(transaction: BankTransaction) -> dict[str, Any]:
        return {
            "id": transaction.id,
            "bank_account_id": transaction.bank_account_id,
            "posted_date": transaction.posted_date.isoformat(),
            "amount": transaction.amount.to_dict(),
            "description": transaction.description,
            "external_id": transaction.external_id,
            "status": transaction.status.value,
            "matched_entry_id": transaction.matched_entry_id,
            "metadata": transaction.metadata,
        }

    @staticmethod
    def _row(row: Any) -> BankTransaction:
        return BankTransaction(
            id=str(row["id"]),
            bank_account_id=str(row["bank_account_id"]),
            posted_date=parse_date(str(row["posted_date"])),
            amount=Money.from_minor(int(row["amount_minor"]), str(row["currency"])),
            description=str(row["description"]),
            external_id=str(row["external_id"]),
            status=BankTxnStatus(str(row["status"])),
            matched_entry_id=None
            if row["matched_entry_id"] is None
            else str(row["matched_entry_id"]),
            metadata=loads(str(row["metadata_json"])),
        )
