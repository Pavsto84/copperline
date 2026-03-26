"""Append-only operational audit trail."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .db import Database
from .ids import new_id
from .jsonutil import dumps, loads


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        actor: str,
        action: str,
        target: str,
        detail: Any = None,
        *,
        created_at: datetime | None = None,
    ) -> str:
        event_id = new_id("aud")
        timestamp = (created_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
        self.db.execute(
            """
            INSERT INTO audit_events(id, actor, action, target, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, actor, action, target, dumps(detail), timestamp),
        )
        return event_id

    def list(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        target_prefix: str | None = None,
        limit: int = 100,
        after_seq: int = 0,
    ) -> list[dict[str, Any]]:
        where = ["seq > ?"]
        params: list[Any] = [after_seq]
        if actor is not None:
            where.append("actor = ?")
            params.append(actor)
        if action is not None:
            where.append("action = ?")
            params.append(action)
        if target_prefix is not None:
            where.append("target LIKE ?")
            params.append(f"{target_prefix}%")
        params.append(max(1, min(int(limit), 5000)))
        rows = self.db.all(
            f"""
            SELECT seq, id, actor, action, target, detail_json, created_at
            FROM audit_events
            WHERE {' AND '.join(where)}
            ORDER BY seq
            LIMIT ?
            """,
            tuple(params),
        )
        return [
            {
                "seq": int(row["seq"]),
                "id": str(row["id"]),
                "actor": str(row["actor"]),
                "action": str(row["action"]),
                "target": str(row["target"]),
                "detail": loads(str(row["detail_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM audit_events", default=0))
