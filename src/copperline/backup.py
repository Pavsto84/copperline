"""Consistent SQLite backups and portable manifest generation."""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import Database
from .errors import ValidationError


class BackupService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def sqlite_backup(self, target: str | Path) -> Path:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db.backup_to(path)
        return path

    def archive(
        self,
        target: str | Path,
        *,
        extra_files: list[str | Path] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="copperline-backup-") as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "copperline.sqlite"
            self.sqlite_backup(db_path)
            integrity = self._check_backup(db_path)
            manifest = {
                "format": "copperline-backup-v1",
                "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "database_sha256": self._sha256(db_path),
                "database_bytes": db_path.stat().st_size,
                "integrity_check": integrity,
                "schema_versions": [
                    int(row["version"])
                    for row in self.db.all(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ],
                "metadata": metadata or {},
                "extra_files": [],
            }
            copied: list[Path] = []
            for item in extra_files or []:
                source = Path(item)
                if not source.is_file():
                    raise ValidationError(f"backup extra file does not exist: {source}")
                destination = tmp_path / "extra" / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
                copied.append(destination)
                manifest["extra_files"].append(
                    {
                        "name": destination.name,
                        "sha256": self._sha256(destination),
                        "bytes": destination.stat().st_size,
                    }
                )
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            )
            with tarfile.open(target_path, "w:gz") as archive:
                archive.add(db_path, arcname="copperline.sqlite")
                archive.add(manifest_path, arcname="manifest.json")
                for item in copied:
                    archive.add(item, arcname=f"extra/{item.name}")
        return target_path

    @staticmethod
    def verify_archive(path: str | Path) -> dict[str, Any]:
        archive_path = Path(path)
        if not archive_path.is_file():
            raise ValidationError(f"backup archive does not exist: {archive_path}")
        with tempfile.TemporaryDirectory(prefix="copperline-verify-") as tmp:
            tmp_path = Path(tmp)
            with tarfile.open(archive_path, "r:gz") as archive:
                members = {member.name: member for member in archive.getmembers()}
                if "manifest.json" not in members or "copperline.sqlite" not in members:
                    raise ValidationError("backup archive is missing required files")
                for member in members.values():
                    resolved = (tmp_path / member.name).resolve()
                    if tmp_path.resolve() not in resolved.parents and resolved != tmp_path.resolve():
                        raise ValidationError("backup archive contains unsafe path")
                    if member.isdir():
                        resolved.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise ValidationError(
                            f"backup archive contains unsupported member: {member.name}"
                        )
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValidationError(
                            f"backup archive member cannot be read: {member.name}"
                        )
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    with resolved.open("wb") as target_handle:
                        target_handle.write(source.read())
            manifest = json.loads((tmp_path / "manifest.json").read_text())
            db_path = tmp_path / "copperline.sqlite"
            actual = BackupService._sha256(db_path)
            valid_hash = hmac_compare(actual, str(manifest.get("database_sha256", "")))
            extra_results = []
            for item in manifest.get("extra_files", []):
                extra = tmp_path / "extra" / str(item["name"])
                extra_results.append(
                    {
                        "name": item["name"],
                        "valid": extra.is_file()
                        and hmac_compare(
                            BackupService._sha256(extra), str(item["sha256"])
                        ),
                    }
                )
            return {
                "valid": valid_hash and all(item["valid"] for item in extra_results),
                "database_hash_valid": valid_hash,
                "extras": extra_results,
                "manifest": manifest,
            }

    @staticmethod
    def _check_backup(path: Path) -> list[str]:
        import sqlite3

        conn = sqlite3.connect(path)
        try:
            return [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        finally:
            conn.close()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode(), right.encode())
