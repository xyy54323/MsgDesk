"""Daily database backup and configurable media retention cleanup."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from ..config import AppConfig
from ..repository import Repository
from ..timeutil import iso_utc, local_date_of, local_day_bounds, utc_now
from .normalize import content_hash

REF_RE = re.compile(r"/(?:media|attachments)/[^\s)\"'<>]+")
KEEP_BACKUPS = 7
ORPHAN_GRACE_HOURS = 24


class MaintenanceService:
    def __init__(self, config: AppConfig, repo: Repository) -> None:
        self.config = config
        self.repo = repo

    def run(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        stats.update(self.backup_database())
        stats.update(self.clean_orphans())
        stats.update(self.apply_retention())
        return stats

    def backup_database(self) -> dict[str, Any]:
        """Keep one SQLite snapshot per day, retaining the newest seven."""
        today = local_date_of(utc_now().isoformat(), self.config.system.timezone)
        backup_dir = self.config.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"messages-{today}.db"
        if not target.exists() and self.repo.db.path.exists():
            source = sqlite3.connect(self.repo.db.path)
            sink = sqlite3.connect(target)
            try:
                source.backup(sink)
            finally:
                sink.close()
                source.close()
        backups = sorted(backup_dir.glob("messages-*.db"))
        for old in backups[:-KEEP_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                continue
        return {"backup_path": str(target) if target.exists() else ""}

    def clean_orphans(self) -> dict[str, Any]:
        """Delete files no surviving message references, after a grace period."""
        referenced = self._referenced_paths()
        cutoff = utc_now().timestamp() - ORPHAN_GRACE_HOURS * 3600
        removed = 0
        freed = 0
        for root_name in ("media", "attachments"):
            root = self.config.data_dir / root_name
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                ref = f"/{root_name}/{path.relative_to(root).as_posix()}"
                if ref in referenced:
                    continue
                try:
                    stat = path.stat()
                    if stat.st_mtime > cutoff:
                        continue
                    path.unlink()
                except OSError:
                    continue
                removed += 1
                freed += stat.st_size
        return {"orphans_removed": removed, "orphans_bytes": freed}

    def apply_retention(self) -> dict[str, Any]:
        days = int(getattr(self.config.system, "retention_days", 0) or 0)
        if days <= 0:
            return {"retained_removed": 0, "retained_bytes": 0, "retained_messages": 0}
        today = date.fromisoformat(
            local_date_of(utc_now().isoformat(), self.config.system.timezone)
        )
        cutoff = today - timedelta(days=days)
        messages_deleted = self._delete_old_messages(cutoff)
        removed_refs: set[str] = set()
        removed = 0
        freed = 0
        for root_name in ("media", "attachments"):
            root = self.config.data_dir / root_name
            if not root.is_dir():
                continue
            for folder in root.iterdir():
                if not folder.is_dir():
                    continue
                try:
                    folder_date = date.fromisoformat(folder.name)
                except ValueError:
                    continue
                if folder_date >= cutoff:
                    continue
                for path in folder.rglob("*"):
                    if not path.is_file():
                        continue
                    try:
                        size = path.stat().st_size
                        path.unlink()
                    except OSError:
                        continue
                    removed_refs.add(
                        f"/{root_name}/{path.relative_to(root).as_posix()}"
                    )
                    removed += 1
                    freed += size
        removed_raw, freed_raw = self._prune_raw(cutoff)
        removed += removed_raw
        freed += freed_raw
        if removed_refs:
            self._strip_references(removed_refs)
        return {
            "retained_removed": removed,
            "retained_bytes": freed,
            "retained_messages": messages_deleted,
        }

    def _delete_old_messages(self, cutoff: date) -> int:
        start, _ = local_day_bounds(cutoff.isoformat(), self.config.system.timezone)
        with self.repo.db.connection() as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM messages WHERE deleted_at IS NULL "
                    "AND COALESCE(published_at, collected_at) < ?",
                    (start,),
                )
            ]
            if not ids:
                return 0
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM jobs WHERE message_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM model_calls WHERE message_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids)
        return len(ids)

    def _prune_raw(self, cutoff: date) -> tuple[int, int]:
        root = self.config.data_dir / "raw"
        if not root.is_dir():
            return 0, 0
        removed = 0
        freed = 0
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            parts = path.relative_to(root).parts
            if len(parts) < 4:
                continue
            try:
                folder_date = date(int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                continue
            if folder_date >= cutoff:
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            removed += 1
            freed += size
        return removed, freed

    def _referenced_paths(self) -> set[str]:
        with self.repo.db.connection() as conn:
            rows = conn.execute(
                "SELECT content_text FROM messages WHERE deleted_at IS NULL"
            ).fetchall()
        refs: set[str] = set()
        for row in rows:
            refs.update(REF_RE.findall(row["content_text"] or ""))
        return refs

    def _strip_references(self, removed: set[str]) -> None:
        """Drop markdown lines that point at files removed by retention."""
        with self.repo.db.connection() as conn:
            rows = conn.execute(
                "SELECT id, content_text FROM messages WHERE deleted_at IS NULL"
            ).fetchall()
            for row in rows:
                text = str(row["content_text"] or "")
                if not any(ref in text for ref in removed):
                    continue
                lines = [
                    line
                    for line in text.split("\n")
                    if not any(ref in line for ref in removed)
                ]
                cleaned = "\n".join(lines)
                cleaned = re.sub(r"## 附件\s*\n+(?=(## |$|>))", "", cleaned)
                cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
                if cleaned == text:
                    continue
                conn.execute(
                    "UPDATE messages SET content_text = ?, content_hash = ?, updated_at = ? "
                    "WHERE id = ?",
                    (cleaned, content_hash(cleaned), iso_utc(), row["id"]),
                )
