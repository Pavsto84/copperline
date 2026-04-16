"""Bank-to-ledger matching and statement reconciliation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from .audit import AuditService
from .banking import BankingService
from .db import Database
from .errors import ConflictError, ReconciliationError, ValidationError
from .ids import new_id
from .ledger import LedgerService
from .models import BankTransaction, BankTxnStatus, JournalEntry
from .money import Money
from .timeutil import parse_date


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    entry_id: str
    score: Decimal
    amount_score: Decimal
    date_score: Decimal
    text_score: Decimal
    amount: Money
    date: date
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "score": format(self.score, "f"),
            "amount_score": format(self.amount_score, "f"),
            "date_score": format(self.date_score, "f"),
            "text_score": format(self.text_score, "f"),
            "amount": self.amount.to_dict(),
            "date": self.date.isoformat(),
            "description": self.description,
        }


class ReconciliationService:
    def __init__(
        self,
        db: Database,
        banking: BankingService,
        ledger: LedgerService,
        audit: AuditService | None = None,
    ) -> None:
        self.db = db
        self.banking = banking
        self.ledger = ledger
        self.audit = audit or AuditService(db)

    def candidates(
        self,
        transaction_id: str,
        *,
        date_window_days: int = 7,
        limit: int = 20,
    ) -> list[MatchCandidate]:
        transaction = self.banking.get_transaction(transaction_id)
        if transaction.status != BankTxnStatus.UNMATCHED:
            return []
        bank = self.banking.get_account(transaction.bank_account_id)
        entries = self.ledger.list(
            start=transaction.posted_date.fromordinal(
                transaction.posted_date.toordinal() - max(0, date_window_days)
            ),
            end=transaction.posted_date.fromordinal(
                transaction.posted_date.toordinal() + max(0, date_window_days)
            ),
            status="posted",
            account_id=bank.ledger_account_id,
            limit=2000,
        )
        result: list[MatchCandidate] = []
        for entry in entries:
            amount = self._entry_bank_effect(entry, bank.ledger_account_id)
            candidate = self._score(transaction, entry, amount, date_window_days)
            if candidate.score > 0:
                result.append(candidate)
        result.sort(key=lambda item: (-item.score, abs((item.date - transaction.posted_date).days), item.entry_id))
        return result[: max(1, min(limit, 100))]

    def _entry_bank_effect(self, entry: JournalEntry, account_id: str) -> Money:
        debit = Money.zero(entry.currency)
        credit = Money.zero(entry.currency)
        for line in entry.lines:
            if line.account_id == account_id:
                debit += line.debit
                credit += line.credit
        return debit - credit

    def _score(
        self,
        transaction: BankTransaction,
        entry: JournalEntry,
        entry_amount: Money,
        window: int,
    ) -> MatchCandidate:
        if entry_amount.currency != transaction.amount.currency:
            amount_score = Decimal(0)
        else:
            difference = abs(entry_amount.minor_units - transaction.amount.minor_units)
            if difference == 0:
                amount_score = Decimal(1)
            else:
                denominator = max(abs(transaction.amount.minor_units), 1)
                relative = Decimal(difference) / Decimal(denominator)
                amount_score = max(Decimal(0), Decimal(1) - relative * Decimal(5))
        days = abs((entry.date - transaction.posted_date).days)
        if window <= 0:
            date_score = Decimal(1) if days == 0 else Decimal(0)
        else:
            date_score = max(Decimal(0), Decimal(1) - Decimal(days) / Decimal(window + 1))
        left = self._normalize_text(transaction.description)
        right = self._normalize_text(f"{entry.description} {entry.reference}")
        text_score = Decimal(str(SequenceMatcher(None, left, right).ratio()))
        score = (
            amount_score * Decimal("0.65")
            + date_score * Decimal("0.20")
            + text_score * Decimal("0.15")
        )
        return MatchCandidate(
            entry_id=entry.id,
            score=score.quantize(Decimal("0.0001")),
            amount_score=amount_score.quantize(Decimal("0.0001")),
            date_score=date_score.quantize(Decimal("0.0001")),
            text_score=text_score.quantize(Decimal("0.0001")),
            amount=entry_amount,
            date=entry.date,
            description=entry.description,
        )

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.lower()
        text = re.sub(r"\b\d{4,}\b", " ", text)
        text = re.sub(r"[^a-z0-9]+", " ", text)
        tokens = [token for token in text.split() if len(token) > 1]
        return " ".join(tokens)

    def match(
        self,
        transaction_id: str,
        entry_id: str,
        *,
        actor: str = "system",
        allow_amount_difference: bool = False,
    ) -> BankTransaction:
        with self.db.transaction():
            transaction = self.banking.get_transaction(transaction_id)
            if transaction.status != BankTxnStatus.UNMATCHED:
                raise ConflictError("bank transaction is not unmatched")
            bank = self.banking.get_account(transaction.bank_account_id)
            entry = self.ledger.get(entry_id)
            if entry.status.value != "posted":
                raise ConflictError("journal entry is not posted")
            effect = self._entry_bank_effect(entry, bank.ledger_account_id)
            if effect.currency != transaction.amount.currency:
                raise ReconciliationError("bank and journal entry currencies differ")
            if effect != transaction.amount and not allow_amount_difference:
                raise ReconciliationError(
                    "bank and journal entry amounts differ",
                    details={
                        "bank": transaction.amount.to_dict(),
                        "ledger": effect.to_dict(),
                    },
                )
            other = self.db.one(
                """
                SELECT id FROM bank_transactions
                WHERE matched_entry_id=? AND status='matched' AND id != ?
                """,
                (entry.id, transaction.id),
            )
            if other is not None:
                raise ConflictError(
                    f"journal entry is already matched to bank transaction {other['id']}"
                )
            self.db.execute(
                """
                UPDATE bank_transactions
                SET status='matched', matched_entry_id=?
                WHERE id=?
                """,
                (entry.id, transaction.id),
            )
            transaction.status = BankTxnStatus.MATCHED
            transaction.matched_entry_id = entry.id
            self.audit.record(
                actor,
                "bank.match",
                transaction.id,
                {
                    "entry_id": entry.id,
                    "bank_amount": transaction.amount.to_dict(),
                    "ledger_amount": effect.to_dict(),
                },
            )
            return transaction

    def auto_match(
        self,
        bank_account_id: str,
        *,
        threshold: Decimal | str | float = Decimal("0.92"),
        margin: Decimal | str | float = Decimal("0.08"),
        actor: str = "system",
    ) -> dict[str, Any]:
        threshold_value = Decimal(str(threshold))
        margin_value = Decimal(str(margin))
        if not Decimal(0) <= threshold_value <= Decimal(1):
            raise ValidationError("auto-match threshold must be 0..1")
        matched: list[dict[str, Any]] = []
        ambiguous: list[str] = []
        for transaction in self.banking.list_transactions(
            bank_account_id, status=BankTxnStatus.UNMATCHED
        ):
            candidates = self.candidates(transaction.id)
            if not candidates:
                continue
            top = candidates[0]
            runner = candidates[1] if len(candidates) > 1 else None
            if top.score < threshold_value:
                continue
            if runner is not None and top.score - runner.score < margin_value:
                ambiguous.append(transaction.id)
                continue
            try:
                self.match(transaction.id, top.entry_id, actor=actor)
            except (ConflictError, ReconciliationError):
                ambiguous.append(transaction.id)
                continue
            matched.append({"transaction_id": transaction.id, "entry_id": top.entry_id, "score": format(top.score, "f")})
        return {"matched": matched, "ambiguous": ambiguous}

    def start_session(
        self,
        bank_account_id: str,
        statement_date: str | date,
        statement_balance: Money,
    ) -> str:
        bank = self.banking.get_account(bank_account_id)
        if statement_balance.currency != bank.currency:
            raise ValidationError("statement balance currency mismatch")
        existing = self.db.one(
            """
            SELECT id FROM reconciliation_sessions
            WHERE bank_account_id=? AND status='open'
            """,
            (bank.id,),
        )
        if existing is not None:
            raise ConflictError(f"bank account already has open reconciliation {existing['id']}")
        session_id = new_id("rec")
        self.db.execute(
            """
            INSERT INTO reconciliation_sessions(
                id, bank_account_id, statement_date, statement_balance_minor,
                currency, status, started_at
            ) VALUES (?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                session_id,
                bank.id,
                parse_date(statement_date).isoformat(),
                statement_balance.minor_units,
                statement_balance.currency,
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )
        return session_id

    def session_status(self, session_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM reconciliation_sessions WHERE id=?", (session_id,))
        if row is None:
            raise NotFoundError("reconciliation session", session_id)
        bank = self.banking.get_account(str(row["bank_account_id"]))
        statement_date = parse_date(str(row["statement_date"]))
        ledger_balance = self.ledger.balance(bank.ledger_account_id, as_of=statement_date)
        statement = Money.from_minor(int(row["statement_balance_minor"]), str(row["currency"]))
        difference = ledger_balance - statement
        unmatched = [
            item
            for item in self.banking.list_transactions(
                bank.id,
                status=BankTxnStatus.UNMATCHED,
                end=statement_date,
            )
        ]
        return {
            "id": session_id,
            "bank_account_id": bank.id,
            "statement_date": statement_date.isoformat(),
            "statement_balance": statement.to_dict(),
            "ledger_balance": ledger_balance.to_dict(),
            "difference": difference.to_dict(),
            "unmatched_count": len(unmatched),
            "status": str(row["status"]),
            "started_at": str(row["started_at"]),
            "completed_at": row["completed_at"],
        }

    def complete_session(
        self,
        session_id: str,
        *,
        actor: str = "system",
        tolerance_minor: int = 0,
        require_all_matched: bool = True,
    ) -> dict[str, Any]:
        status = self.session_status(session_id)
        difference = Money(
            status["difference"]["amount"],
            status["difference"]["currency"],
        )
        if abs(difference.minor_units) > max(0, tolerance_minor):
            raise ReconciliationError(
                "statement does not reconcile to ledger",
                details={"difference": difference.to_dict()},
            )
        if require_all_matched and int(status["unmatched_count"]) > 0:
            raise ReconciliationError(
                "statement has unmatched bank transactions",
                details={"unmatched_count": status["unmatched_count"]},
            )
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.db.execute(
            """
            UPDATE reconciliation_sessions
            SET status='completed', completed_at=?
            WHERE id=? AND status='open'
            """,
            (now, session_id),
        )
        self.audit.record(actor, "reconciliation.complete", session_id, status)
        return self.session_status(session_id)
