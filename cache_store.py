from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass(slots=True)
class CachedMessage:
    scope_key: str
    platform_id: str
    platform_name: str
    session_id: str
    group_id: str
    message_type: str
    astr_message_id: str
    original_message_id: str
    ref_index: str
    content: str
    sender_id: str
    sender_name: str
    timestamp: int
    is_bot: bool = False
    attachments: list[dict[str, Any]] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)
    raw_meta: dict[str, Any] = field(default_factory=dict)
    cached_at: int = 0
    expires_at: int = 0
    db_id: int | None = None


class MessageStore:
    """Thread-safe SQLite message cache with scoped aliases."""

    def __init__(self, db_path: Path, ttl_seconds: int, max_entries: int = 50000):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(int(ttl_seconds), 60)
        self.max_entries = max(int(max_entries), 100)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_key TEXT NOT NULL,
                platform_id TEXT NOT NULL DEFAULT '',
                platform_name TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                message_type TEXT NOT NULL DEFAULT '',
                astr_message_id TEXT NOT NULL DEFAULT '',
                original_message_id TEXT NOT NULL DEFAULT '',
                ref_index TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                sender_id TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                timestamp INTEGER NOT NULL,
                is_bot INTEGER NOT NULL DEFAULT 0,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                components_json TEXT NOT NULL DEFAULT '[]',
                raw_meta_json TEXT NOT NULL DEFAULT '{}',
                cached_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_scope_time
                ON messages(scope_key, cached_at DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_expiry
                ON messages(expires_at);
            CREATE TABLE IF NOT EXISTS aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                scope_key TEXT NOT NULL,
                alias TEXT NOT NULL,
                alias_kind TEXT NOT NULL DEFAULT 'unknown',
                UNIQUE(message_id, scope_key, alias)
            );
            CREATE INDEX IF NOT EXISTS idx_alias_lookup
                ON aliases(scope_key, alias);
            """
        )
        self._db.commit()

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load(value: str, default: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default

    def put(
        self,
        message: CachedMessage,
        aliases: Iterable[tuple[str, str]],
    ) -> int:
        now = int(time.time())
        message.cached_at = message.cached_at or now
        message.expires_at = message.expires_at or (message.cached_at + self.ttl_seconds)
        normalized: dict[str, str] = {}
        for alias, kind in aliases:
            alias = str(alias or "").strip()
            if alias:
                normalized[alias] = str(kind or "unknown")
        if message.astr_message_id:
            normalized.setdefault(message.astr_message_id, "astr_message_id")

        with self._lock:
            existing = None
            if message.astr_message_id:
                existing = self._db.execute(
                    "SELECT id FROM messages WHERE scope_key=? AND astr_message_id=? "
                    "ORDER BY cached_at DESC LIMIT 1",
                    (message.scope_key, message.astr_message_id),
                ).fetchone()
            values = (
                message.scope_key,
                message.platform_id,
                message.platform_name,
                message.session_id,
                message.group_id,
                message.message_type,
                message.astr_message_id,
                message.original_message_id,
                message.ref_index,
                message.content,
                message.sender_id,
                message.sender_name,
                int(message.timestamp or now),
                int(bool(message.is_bot)),
                self._dump(message.attachments),
                self._dump(message.components),
                self._dump(message.raw_meta),
                message.cached_at,
                message.expires_at,
            )
            if existing:
                message_id = int(existing["id"])
                self._db.execute(
                    """UPDATE messages SET
                    platform_id=?, platform_name=?, session_id=?, group_id=?, message_type=?,
                    original_message_id=?, ref_index=?, content=?, sender_id=?, sender_name=?,
                    timestamp=?, is_bot=?, attachments_json=?, components_json=?, raw_meta_json=?,
                    cached_at=?, expires_at=? WHERE id=?""",
                    values[1:6] + values[7:] + (message_id,),
                )
            else:
                cur = self._db.execute(
                    """INSERT INTO messages (
                    scope_key, platform_id, platform_name, session_id, group_id, message_type,
                    astr_message_id, original_message_id, ref_index, content, sender_id,
                    sender_name, timestamp, is_bot, attachments_json, components_json,
                    raw_meta_json, cached_at, expires_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
                message_id = int(cur.lastrowid)
            for alias, kind in normalized.items():
                self._db.execute(
                    "INSERT OR REPLACE INTO aliases(message_id, scope_key, alias, alias_kind) "
                    "VALUES(?,?,?,?)",
                    (message_id, message.scope_key, alias, kind),
                )
            self._db.commit()
            self._prune_locked(now)
            return message_id

    def _row_to_message(self, row: sqlite3.Row) -> CachedMessage:
        return CachedMessage(
            db_id=int(row["id"]),
            scope_key=row["scope_key"],
            platform_id=row["platform_id"],
            platform_name=row["platform_name"],
            session_id=row["session_id"],
            group_id=row["group_id"],
            message_type=row["message_type"],
            astr_message_id=row["astr_message_id"],
            original_message_id=row["original_message_id"],
            ref_index=row["ref_index"],
            content=row["content"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            timestamp=int(row["timestamp"]),
            is_bot=bool(row["is_bot"]),
            attachments=self._load(row["attachments_json"], []),
            components=self._load(row["components_json"], []),
            raw_meta=self._load(row["raw_meta_json"], {}),
            cached_at=int(row["cached_at"]),
            expires_at=int(row["expires_at"]),
        )

    def find(self, scope_key: str, aliases: Iterable[str]) -> CachedMessage | None:
        now = int(time.time())
        candidates = [str(x).strip() for x in aliases if str(x or "").strip()]
        if not candidates:
            return None
        placeholders = ",".join("?" for _ in candidates)
        with self._lock:
            row = self._db.execute(
                f"""SELECT m.* FROM aliases a JOIN messages m ON m.id=a.message_id
                WHERE a.scope_key=? AND a.alias IN ({placeholders}) AND m.expires_at>?
                ORDER BY m.cached_at DESC LIMIT 1""",
                (scope_key, *candidates, now),
            ).fetchone()
            return self._row_to_message(row) if row else None

    def aliases_for(self, message_id: int) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT alias, alias_kind FROM aliases WHERE message_id=? ORDER BY id",
                (message_id,),
            ).fetchall()
        return [{"alias": r["alias"], "kind": r["alias_kind"]} for r in rows]

    def cleanup_expired(self) -> tuple[int, list[str]]:
        now = int(time.time())
        with self._lock:
            rows = self._db.execute(
                "SELECT attachments_json FROM messages WHERE expires_at<=?", (now,)
            ).fetchall()
            paths = self._attachment_paths(rows)
            cur = self._db.execute("DELETE FROM messages WHERE expires_at<=?", (now,))
            paths = self._orphaned_paths_locked(paths)
            self._db.commit()
            return int(cur.rowcount), paths

    def clear(self, scope_key: str | None = None) -> tuple[int, list[str]]:
        with self._lock:
            if scope_key:
                rows = self._db.execute(
                    "SELECT attachments_json FROM messages WHERE scope_key=?", (scope_key,)
                ).fetchall()
                paths = self._attachment_paths(rows)
                cur = self._db.execute("DELETE FROM messages WHERE scope_key=?", (scope_key,))
            else:
                rows = self._db.execute("SELECT attachments_json FROM messages").fetchall()
                paths = self._attachment_paths(rows)
                cur = self._db.execute("DELETE FROM messages")
            paths = self._orphaned_paths_locked(paths)
            self._db.commit()
            return int(cur.rowcount), paths

    def _attachment_paths(self, rows: Iterable[sqlite3.Row]) -> list[str]:
        paths: list[str] = []
        for row in rows:
            for att in self._load(row["attachments_json"], []):
                path = att.get("local_path") if isinstance(att, dict) else None
                if path:
                    paths.append(str(path))
        return paths

    def _orphaned_paths_locked(self, candidates: Iterable[str]) -> list[str]:
        candidate_set = {str(path) for path in candidates if path}
        if not candidate_set:
            return []
        remaining_rows = self._db.execute(
            "SELECT attachments_json FROM messages"
        ).fetchall()
        still_used = set(self._attachment_paths(remaining_rows))
        return sorted(candidate_set - still_used)

    def _prune_locked(self, now: int) -> None:
        self._db.execute("DELETE FROM messages WHERE expires_at<=?", (now,))
        count = int(self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        overflow = count - self.max_entries
        if overflow > 0:
            self._db.execute(
                "DELETE FROM messages WHERE id IN "
                "(SELECT id FROM messages ORDER BY cached_at ASC LIMIT ?)",
                (overflow,),
            )
        self._db.commit()

    def stats(self, scope_key: str | None = None) -> dict[str, Any]:
        now = int(time.time())
        with self._lock:
            if scope_key:
                count = int(
                    self._db.execute(
                        "SELECT COUNT(*) FROM messages WHERE scope_key=? AND expires_at>?",
                        (scope_key, now),
                    ).fetchone()[0]
                )
            else:
                count = int(
                    self._db.execute(
                        "SELECT COUNT(*) FROM messages WHERE expires_at>?", (now,)
                    ).fetchone()[0]
                )
            aliases = int(self._db.execute("SELECT COUNT(*) FROM aliases").fetchone()[0])
        return {
            "messages": count,
            "aliases": aliases,
            "ttl_seconds": self.ttl_seconds,
            "max_entries": self.max_entries,
            "db_path": str(self.db_path),
            "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()
