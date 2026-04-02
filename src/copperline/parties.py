"""Customers, vendors, and contact/account defaults."""

from __future__ import annotations

from typing import Any

from .audit import AuditService
from .db import Database
from .errors import NotFoundError, ValidationError
from .ids import new_id
from .jsonutil import dumps, loads
from .models import Party, PartyKind
from .money import normalize_currency


class PartyService:
    def __init__(self, db: Database, audit: AuditService | None = None) -> None:
        self.db = db
        self.audit = audit or AuditService(db)

    def create(
        self,
        name: str,
        kind: PartyKind | str,
        currency: str,
        *,
        email: str = "",
        phone: str = "",
        tax_id: str = "",
        payment_terms_days: int = 30,
        receivable_account_id: str | None = None,
        payable_account_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> Party:
        name = name.strip()
        if not name:
            raise ValidationError("party name may not be empty")
        if payment_terms_days < 0 or payment_terms_days > 3650:
            raise ValidationError("payment terms must be 0..3650 days")
        party = Party(
            id=new_id("pty"),
            name=name,
            kind=PartyKind(kind),
            currency=normalize_currency(currency),
            email=email.strip(),
            phone=phone.strip(),
            tax_id=tax_id.strip(),
            payment_terms_days=payment_terms_days,
            receivable_account_id=receivable_account_id,
            payable_account_id=payable_account_id,
            metadata=metadata or {},
        )
        self.db.execute(
            """
            INSERT INTO parties(
                id, name, kind, currency, email, phone, tax_id, payment_terms_days,
                receivable_account_id, payable_account_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                party.id,
                party.name,
                party.kind.value,
                party.currency,
                party.email,
                party.phone,
                party.tax_id,
                party.payment_terms_days,
                party.receivable_account_id,
                party.payable_account_id,
                dumps(party.metadata),
            ),
        )
        self.audit.record(actor, "party.create", party.id, self.to_dict(party))
        return party

    def get(self, party_id: str) -> Party:
        row = self.db.one("SELECT * FROM parties WHERE id=?", (party_id,))
        if row is None:
            raise NotFoundError("party", party_id)
        return self._row(row)

    def list(
        self,
        *,
        kind: PartyKind | str | None = None,
        search: str | None = None,
        limit: int = 1000,
    ) -> list[Party]:
        clauses = ["1=1"]
        params: list[Any] = []
        if kind is not None:
            value = PartyKind(kind)
            if value == PartyKind.CUSTOMER:
                clauses.append("kind IN ('customer','both')")
            elif value == PartyKind.VENDOR:
                clauses.append("kind IN ('vendor','both')")
            else:
                clauses.append("kind='both'")
        if search:
            clauses.append("(LOWER(name) LIKE ? OR LOWER(email) LIKE ? OR tax_id LIKE ?)")
            query = f"%{search.lower()}%"
            params.extend([query, query, f"%{search}%"])
        params.append(max(1, min(limit, 5000)))
        return [
            self._row(row)
            for row in self.db.all(
                f"""
                SELECT * FROM parties
                WHERE {' AND '.join(clauses)}
                ORDER BY name, id
                LIMIT ?
                """,
                tuple(params),
            )
        ]

    def update(self, party_id: str, *, actor: str = "system", **changes: Any) -> Party:
        party = self.get(party_id)
        allowed = {
            "name",
            "kind",
            "currency",
            "email",
            "phone",
            "tax_id",
            "payment_terms_days",
            "receivable_account_id",
            "payable_account_id",
            "metadata",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(f"unknown party fields: {', '.join(sorted(unknown))}")
        for key, value in changes.items():
            if key == "kind":
                value = PartyKind(value)
            elif key == "currency":
                value = normalize_currency(value)
            setattr(party, key, value)
        if not party.name.strip():
            raise ValidationError("party name may not be empty")
        self.db.execute(
            """
            UPDATE parties SET
                name=?, kind=?, currency=?, email=?, phone=?, tax_id=?,
                payment_terms_days=?, receivable_account_id=?, payable_account_id=?,
                metadata_json=?
            WHERE id=?
            """,
            (
                party.name,
                party.kind.value,
                party.currency,
                party.email,
                party.phone,
                party.tax_id,
                party.payment_terms_days,
                party.receivable_account_id,
                party.payable_account_id,
                dumps(party.metadata),
                party.id,
            ),
        )
        self.audit.record(actor, "party.update", party.id, changes)
        return party

    @staticmethod
    def to_dict(party: Party) -> dict[str, Any]:
        return {
            "id": party.id,
            "name": party.name,
            "kind": party.kind.value,
            "currency": party.currency,
            "email": party.email,
            "phone": party.phone,
            "tax_id": party.tax_id,
            "payment_terms_days": party.payment_terms_days,
            "receivable_account_id": party.receivable_account_id,
            "payable_account_id": party.payable_account_id,
            "metadata": party.metadata,
        }

    @staticmethod
    def _row(row: Any) -> Party:
        return Party(
            id=str(row["id"]),
            name=str(row["name"]),
            kind=PartyKind(str(row["kind"])),
            currency=str(row["currency"]),
            email=str(row["email"]),
            phone=str(row["phone"]),
            tax_id=str(row["tax_id"]),
            payment_terms_days=int(row["payment_terms_days"]),
            receivable_account_id=None
            if row["receivable_account_id"] is None
            else str(row["receivable_account_id"]),
            payable_account_id=None
            if row["payable_account_id"] is None
            else str(row["payable_account_id"]),
            metadata=loads(str(row["metadata_json"])),
        )
