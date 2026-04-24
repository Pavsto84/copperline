"""High-entropy API keys with scrypt hashing and scope checks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from typing import Iterable

from .audit import AuditService
from .db import Database
from .errors import AuthenticationError, AuthorizationError, NotFoundError, ValidationError
from .ids import new_id


DEFAULT_SCOPES = (
    "accounts:read",
    "ledger:read",
    "invoices:read",
    "payments:read",
    "inventory:read",
    "banking:read",
    "reports:read",
)


class AuthService:
    def __init__(self, db: Database, audit: AuditService | None = None) -> None:
        self.db = db
        self.audit = audit or AuditService(db)

    def create_key(
        self,
        label: str,
        scopes: Iterable[str] = DEFAULT_SCOPES,
        *,
        actor: str = "system",
    ) -> dict[str, object]:
        label = label.strip()
        if not label:
            raise ValidationError("API key label may not be empty")
        normalized_scopes = sorted(set(self._normalize_scope(scope) for scope in scopes))
        key_id = new_id("key")
        secret = secrets.token_urlsafe(32)
        token = f"cl_{key_id}_{secret}"
        salt = secrets.token_bytes(16)
        digest = self._hash(token, salt)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.db.execute(
            """
            INSERT INTO api_keys(
                id, label, token_hash, salt, scopes_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                key_id,
                label,
                base64.b64encode(digest).decode(),
                base64.b64encode(salt).decode(),
                json.dumps(normalized_scopes),
                now,
            ),
        )
        self.audit.record(
            actor,
            "api_key.create",
            key_id,
            {"label": label, "scopes": normalized_scopes},
        )
        return {
            "id": key_id,
            "label": label,
            "token": token,
            "scopes": normalized_scopes,
            "created_at": now,
        }

    def authenticate(self, token: str) -> dict[str, object]:
        if not token.startswith("cl_key_"):
            raise AuthenticationError("invalid API key format")
        parts = token.split("_", 3)
        if len(parts) != 4:
            raise AuthenticationError("invalid API key format")
        key_id = f"{parts[1]}_{parts[2]}"
        row = self.db.one("SELECT * FROM api_keys WHERE id=?", (key_id,))
        if row is None or row["revoked_at"] is not None:
            raise AuthenticationError("invalid or revoked API key")
        salt = base64.b64decode(str(row["salt"]))
        expected = base64.b64decode(str(row["token_hash"]))
        actual = self._hash(token, salt)
        if not hmac.compare_digest(expected, actual):
            raise AuthenticationError("invalid or revoked API key")
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now, key_id))
        return {
            "id": key_id,
            "label": str(row["label"]),
            "scopes": json.loads(str(row["scopes_json"])),
            "created_at": str(row["created_at"]),
            "last_used_at": now,
        }

    def authorize(self, principal: dict[str, object], scope: str) -> None:
        required = self._normalize_scope(scope)
        scopes = [str(value) for value in principal.get("scopes", [])]
        if "*" in scopes or required in scopes:
            return
        domain = required.split(":", 1)[0]
        if f"{domain}:*" in scopes:
            return
        raise AuthorizationError(f"API key lacks scope {required}")

    def list_keys(self) -> list[dict[str, object]]:
        return [
            {
                "id": str(row["id"]),
                "label": str(row["label"]),
                "scopes": json.loads(str(row["scopes_json"])),
                "created_at": str(row["created_at"]),
                "last_used_at": row["last_used_at"],
                "revoked_at": row["revoked_at"],
            }
            for row in self.db.all(
                "SELECT * FROM api_keys ORDER BY created_at DESC, id DESC"
            )
        ]

    def revoke(self, key_id: str, *, actor: str = "system") -> bool:
        row = self.db.one("SELECT revoked_at FROM api_keys WHERE id=?", (key_id,))
        if row is None:
            raise NotFoundError("API key", key_id)
        if row["revoked_at"] is not None:
            return False
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.db.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (now, key_id))
        self.audit.record(actor, "api_key.revoke", key_id)
        return True

    @staticmethod
    def _hash(token: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            token.encode(),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )

    @staticmethod
    def _normalize_scope(scope: str) -> str:
        value = str(scope).strip().lower()
        if value == "*":
            return value
        if ":" not in value:
            raise ValidationError(f"invalid API scope: {scope}")
        domain, action = value.split(":", 1)
        if not domain or not action or not domain.replace("_", "").isalnum():
            raise ValidationError(f"invalid API scope: {scope}")
        return f"{domain}:{action}"
