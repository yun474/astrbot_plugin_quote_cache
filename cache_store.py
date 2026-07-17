from __future__ import annotations

import json
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


NEVER_EXPIRES_AT = 253_402_300_799


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


@dataclass(slots=True)
class SearchHit:
    message: CachedMessage
    score: float
    match_type: str


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _partial_ratio(query: str, content: str) -> float:
    """Small dependency-free approximation of fuzzy partial matching."""
    query = _normalize(query)
    content = _normalize(content)[:6000]
    if not query or not content:
        return 0.0
    if query in content:
        return 1.0
    if len(query) == 1:
        return 0.0

    matcher = SequenceMatcher(None, query, content, autojunk=False)
    best = 0.0
    window_size = len(query)
    for block in matcher.get_matching_blocks():
        start = max(0, block.b - block.a)
        for offset in (-2, -1, 0, 1, 2):
            window_start = max(0, start + offset)
            window = content[window_start : window_start + window_size]
            if not window:
                continue
            best = max(
                best,
                SequenceMatcher(None, query, window, autojunk=False).ratio(),
            )
    return best


class MessageStore:
    """Thread-safe SQLite message cache with session-scoped history search."""

    def __init__(self, db_path: Path, ttl_seconds: int, max_entries: int = 200000):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(int(ttl_seconds), 0)
        self.max_entries = max(int(max_entries), 100)
        self._lock = threading.RLock()
        self._closed = False
        self._writes_since_prune = 0
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()
            self._prune_locked(int(time.time()))

    def _create_schema(self) -> None:
        # Keep the v0.x quote-cache schema so existing databases remain readable.
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
            CREATE INDEX IF NOT EXISTS idx_messages_scope_timestamp
                ON messages(scope_key, timestamp DESC);
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
        message.expires_at = message.expires_at or (
            message.cached_at + self.ttl_seconds
            if self.ttl_seconds > 0
            else NEVER_EXPIRES_AT
        )
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
            self._writes_since_prune += 1
            if self._writes_since_prune >= 100:
                self._prune_locked(now)
                self._writes_since_prune = 0
            else:
                self._db.commit()
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

    def _base_search_where(
        self,
        scope_key: str,
        sender: str,
        since_timestamp: int,
        exclude_astr_message_id: str,
    ) -> tuple[list[str], list[Any]]:
        clauses = ["scope_key=?", "expires_at>?"]
        params: list[Any] = [scope_key, int(time.time())]
        if sender:
            pattern = f"%{_escape_like(sender)}%"
            clauses.append(
                "(sender_name LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR sender_id LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            params.extend((pattern, pattern))
        if since_timestamp > 0:
            clauses.append("timestamp>=?")
            params.append(int(since_timestamp))
        if exclude_astr_message_id:
            clauses.append("astr_message_id<>?")
            params.append(exclude_astr_message_id)
        return clauses, params

    def search(
        self,
        scope_key: str,
        query: str,
        *,
        limit: int = 8,
        sender: str = "",
        since_timestamp: int = 0,
        fuzzy_threshold: float = 0.58,
        fuzzy_candidate_limit: int = 3000,
        exclude_astr_message_id: str = "",
    ) -> list[SearchHit]:
        query = str(query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 100))
        fuzzy_threshold = max(0.0, min(float(fuzzy_threshold), 1.0))
        fuzzy_candidate_limit = max(100, int(fuzzy_candidate_limit))
        terms = [part for part in _normalize(query).split(" ") if part]

        base_clauses, base_params = self._base_search_where(
            scope_key,
            str(sender or "").strip(),
            since_timestamp,
            exclude_astr_message_id,
        )
        exact_clauses = list(base_clauses)
        exact_params = list(base_params)
        for term in terms:
            exact_clauses.append("content LIKE ? ESCAPE '\\' COLLATE NOCASE")
            exact_params.append(f"%{_escape_like(term)}%")

        with self._lock:
            exact_rows = self._db.execute(
                "SELECT * FROM messages WHERE "
                + " AND ".join(exact_clauses)
                + " ORDER BY timestamp DESC, id DESC LIMIT ?",
                (*exact_params, max(limit * 12, 120)),
            ).fetchall()

            candidate_rows: list[sqlite3.Row] = []
            if len(exact_rows) < limit and len(_normalize(query)) >= 2:
                candidate_rows = self._db.execute(
                    "SELECT * FROM messages WHERE "
                    + " AND ".join(base_clauses)
                    + " ORDER BY timestamp DESC, id DESC LIMIT ?",
                    (*base_params, fuzzy_candidate_limit),
                ).fetchall()

        hits: list[SearchHit] = []
        seen: set[int] = set()
        normalized_query = _normalize(query)
        for row in exact_rows:
            message = self._row_to_message(row)
            normalized_content = _normalize(message.content)
            score = 1.0 if normalized_query in normalized_content else 0.97
            hits.append(SearchHit(message, score, "substring"))
            seen.add(int(row["id"]))

        for row in candidate_rows:
            row_id = int(row["id"])
            if row_id in seen:
                continue
            score = _partial_ratio(query, row["content"])
            if score < fuzzy_threshold:
                continue
            message = self._row_to_message(row)
            hits.append(SearchHit(message, score, "fuzzy"))
            seen.add(row_id)

        hits.sort(
            key=lambda hit: (hit.score, hit.message.timestamp, hit.message.db_id or 0),
            reverse=True,
        )
        return hits[:limit]

    def context(
        self,
        scope_key: str,
        message_id: int,
        *,
        before: int = 3,
        after: int = 3,
    ) -> list[CachedMessage]:
        now = int(time.time())
        before = max(0, min(int(before), 20))
        after = max(0, min(int(after), 20))
        with self._lock:
            anchor = self._db.execute(
                "SELECT * FROM messages WHERE id=? AND scope_key=? AND expires_at>?",
                (int(message_id), scope_key, now),
            ).fetchone()
            if not anchor:
                return []
            older = self._db.execute(
                """SELECT * FROM messages WHERE scope_key=? AND expires_at>?
                AND (timestamp<? OR (timestamp=? AND id<?))
                ORDER BY timestamp DESC, id DESC LIMIT ?""",
                (
                    scope_key,
                    now,
                    int(anchor["timestamp"]),
                    int(anchor["timestamp"]),
                    int(anchor["id"]),
                    before,
                ),
            ).fetchall()
            newer = self._db.execute(
                """SELECT * FROM messages WHERE scope_key=? AND expires_at>?
                AND (timestamp>? OR (timestamp=? AND id>?))
                ORDER BY timestamp ASC, id ASC LIMIT ?""",
                (
                    scope_key,
                    now,
                    int(anchor["timestamp"]),
                    int(anchor["timestamp"]),
                    int(anchor["id"]),
                    after,
                ),
            ).fetchall()
        rows = list(reversed(older)) + [anchor] + list(newer)
        return [self._row_to_message(row) for row in rows]

    def cleanup_expired(self) -> tuple[int, list[str]]:
        now = int(time.time())
        with self._lock:
            cur = self._db.execute("DELETE FROM messages WHERE expires_at<=?", (now,))
            self._db.commit()
            return int(cur.rowcount), []

    def clear(self, scope_key: str | None = None) -> tuple[int, list[str]]:
        with self._lock:
            if scope_key:
                cur = self._db.execute("DELETE FROM messages WHERE scope_key=?", (scope_key,))
            else:
                cur = self._db.execute("DELETE FROM messages")
            self._db.commit()
            return int(cur.rowcount), []

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
            if self._closed:
                return
            self._db.close()
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed
