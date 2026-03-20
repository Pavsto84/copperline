"""SQLite connection, migrations, transactions, and low-level helpers."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import MigrationError
from .jsonutil import dumps, loads
from .migrations import MIGRATIONS


class Database:
    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        timeout: float = 5.0,
        wal: bool = True,
    ) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._conn = sqlite3.connect(
            self.path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        if wal and self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self.migrate()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    @property
    def transaction_depth(self) -> int:
        return int(getattr(self._local, "depth", 0))

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            depth = self.transaction_depth
            savepoint = f"copperline_sp_{depth}"
            try:
                if depth == 0:
                    self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                else:
                    self._conn.execute(f"SAVEPOINT {savepoint}")
                self._local.depth = depth + 1
                yield self._conn
                self._local.depth = depth
                if depth == 0:
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                self._local.depth = depth
                if depth == 0:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                else:
                    self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise

    def migrate(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        applied = {
            int(row["version"])
            for row in self._conn.execute("SELECT version FROM schema_migrations")
        }
        for migration in MIGRATIONS:
            if migration.version in applied:
                continue
            try:
                with self.transaction():
                    for statement in _statements(migration.sql):
                        self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                        (migration.version, migration.name),
                    )
            except sqlite3.DatabaseError as exc:
                raise MigrationError(
                    f"migration {migration.version} ({migration.name}) failed",
                    details={"version": migration.version, "name": migration.name},
                ) from exc

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, rows)

    def one(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, parameters).fetchone()

    def all(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, parameters).fetchall())

    def scalar(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
        *,
        default: Any = None,
    ) -> Any:
        row = self.one(sql, parameters)
        if row is None:
            return default
        return row[0]

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            """
            INSERT INTO settings(key, value_json) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
            """,
            (key, dumps(value)),
        )

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value_json FROM settings WHERE key = ?", (key,))
        return default if row is None else loads(str(row["value_json"]))

    def backup_to(self, target: str | Path) -> None:
        destination = sqlite3.connect(str(target))
        try:
            with self._lock, destination:
                self._conn.backup(destination)
        finally:
            destination.close()

    def integrity_check(self) -> list[str]:
        return [str(row[0]) for row in self._conn.execute("PRAGMA integrity_check")]

    def table_counts(self) -> dict[str, int]:
        tables = [
            str(row["name"])
            for row in self.all(
                """
                SELECT name FROM sqlite_master
                WHERE type='table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        return {
            table: int(self.scalar(f'SELECT COUNT(*) FROM "{table}"', default=0))
            for table in tables
        }


def _statements(script: str) -> list[str]:
    """Split a migration script using sqlite3's own completeness parser."""
    statements: list[str] = []
    buffer: list[str] = []
    for line in script.splitlines():
        buffer.append(line)
        candidate = "\n".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buffer.clear()
    tail = "\n".join(buffer).strip()
    if tail:
        if not sqlite3.complete_statement(tail + ";"):
            raise MigrationError("migration contains an incomplete SQL statement")
        statements.append(tail)
    return statements
