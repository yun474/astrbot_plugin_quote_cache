"""Image cache: bounded persistent storage and public-network downloads."""

import asyncio
import base64
import hashlib
import io
import ipaddress
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from PIL import Image, ImageOps

FORMATS = {
    "PNG": (".png", "image/png"),
    "JPEG": (".jpg", "image/jpeg"),
    "GIF": (".gif", "image/gif"),
    "WEBP": (".webp", "image/webp"),
    "BMP": (".bmp", "image/bmp"),
}
MAX_PIXELS = 25_000_000


def public_address(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
    )


def validate_url(url):
    """Reject credentials, non-HTTP schemes and private IP literals."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("unsupported image URL")
    if parsed.port not in (None, 80, 443):
        raise ValueError("unsupported image URL port")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if parsed.hostname.lower() == "localhost":
            raise ValueError("private image address") from None
    else:
        if not public_address(parsed.hostname):
            raise ValueError("private image address")


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate the exact addresses the HTTP connector will use."""

    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        results = await self.resolver.resolve(host, port, family)
        if not results or any(not public_address(item["host"]) for item in results):
            raise ValueError("private image address")
        return results

    async def close(self):
        await self.resolver.close()


async def read_source(session, ref, *, local_allowed, max_bytes):
    """Read one bounded source; redirects must pass the same network checks."""
    if ref.startswith(("http://", "https://")):
        async with asyncio.timeout(25):
            for _ in range(4):
                validate_url(ref)
                async with session.get(
                    ref,
                    allow_redirects=False,
                    auto_decompress=False,
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("empty image redirect")
                        ref = urljoin(ref, location)
                        continue
                    response.raise_for_status()
                    if (
                        response.headers.get("Content-Encoding", "identity").lower()
                        != "identity"
                    ):
                        raise ValueError(
                            "compressed HTTP image bodies are not supported"
                        )
                    if response.content_length and response.content_length > max_bytes:
                        raise ValueError("image exceeds size limit")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise ValueError("image exceeds size limit")
                    return bytes(data)
            raise ValueError("too many image redirects")
    if not local_allowed:
        raise ValueError("local source not allowed")
    if ref.startswith(("base64://", "data:image/")):
        encoded = ref[9:] if ref.startswith("base64://") else ref.partition(",")[2]
        if len(encoded) > (max_bytes + 2) // 3 * 4:
            raise ValueError("image exceeds size limit")
        data = base64.b64decode(encoded, validate=True)
    else:
        if ref.startswith("file:"):
            parsed = urlsplit(ref)
            if parsed.netloc:
                raise ValueError("network file paths are not supported")
            ref = unquote(parsed.path)
            if os.name == "nt" and len(ref) > 2 and ref[0] == "/" and ref[2] == ":":
                ref = ref[1:]
        if ref.startswith(("\\\\", "//")):
            raise ValueError("network file paths are not supported")

        def read():
            with Path(ref).open("rb") as stream:
                return stream.read(max_bytes + 1)

        data = await asyncio.to_thread(read)
    if len(data) > max_bytes:
        raise ValueError("image exceeds size limit")
    return data


def public_row(row):
    """Expose bot attribution only when true, keeping ordinary rows compact."""
    result = dict(row)
    if result.pop("is_bot"):
        result["sender_is_bot"] = True
    return result


class ImageStore:
    """SQLite indexes are scoped to a platform instance and a conversation.

    Methods are thread-safe and called through asyncio.to_thread by the plugin.
    Files are content-addressed; sightings keep independent IDs and provenance.
    """

    def __init__(
        self, root, *, retention_hours=72, max_bytes=512 * 1024**2, max_entries=10000
    ):
        self.root = Path(root)
        self.images = self.root / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.retention = retention_hours * 3600
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS assets (
                digest TEXT PRIMARY KEY, suffix TEXT NOT NULL,
                size INTEGER NOT NULL, mime TEXT NOT NULL, frames INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sightings (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, message_id TEXT NOT NULL,
                sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
                origin TEXT NOT NULL, sent_at REAL NOT NULL, created REAL NOT NULL,
                digest TEXT NOT NULL, is_bot INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                UNIQUE(scope, message_id, origin, digest)
            );
            CREATE INDEX IF NOT EXISTS by_scope ON sightings(scope, created DESC);
            CREATE INDEX IF NOT EXISTS by_digest ON sightings(digest);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sightings)")}
        if "position" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE sightings ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
                )
        self.prune()

    def store(self, scope, candidate, data, *, seen=None, position=0):
        """Verify original bytes before admission, deduplication and eviction.

        ``seen`` is the message's arrival time and ``position`` the image's place
        in it, so listings follow the chat rather than download completion.
        """
        # Enforce our own lower limit without changing process-wide Pillow settings
        # or warning filters shared with AstrBot and other worker threads.
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in FORMATS or img.width * img.height > MAX_PIXELS:
                raise ValueError("unsupported image or excessive pixel count")
            suffix, mime = FORMATS[img.format]
            frames = 2 if getattr(img, "is_animated", False) else 1
            img.verify()
        if len(data) > self.max_bytes:
            raise ValueError("image exceeds total cache capacity")
        digest = hashlib.sha256(data).hexdigest()
        with self.lock:
            path = self.images / (digest + suffix)
            if not path.exists():
                temporary = self.images / (uuid.uuid4().hex + ".part")
                try:
                    temporary.write_bytes(data)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            now = time.time()
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO assets VALUES (?, ?, ?, ?, ?)",
                    (digest, suffix, len(data), mime, frames),
                )
                self.db.execute(
                    """INSERT INTO sightings (id, scope, message_id, sender_id, sender_name,
                        origin, sent_at, created, digest, is_bot, position)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope, message_id, origin, digest)
                    DO UPDATE SET sender_id=excluded.sender_id,
                        sender_name=excluded.sender_name, sent_at=excluded.sent_at,
                        created=excluded.created, is_bot=excluded.is_bot,
                        position=excluded.position""",
                    (
                        "img_" + uuid.uuid4().hex[:16],
                        scope,
                        candidate.message_id,
                        candidate.sender_id,
                        candidate.sender_name[:120],
                        candidate.origin,
                        candidate.timestamp or now,
                        seen or now,
                        digest,
                        int(candidate.is_bot),
                        position,
                    ),
                )
            row = self.db.execute(
                "SELECT id FROM sightings WHERE scope=? AND message_id=? AND origin=? AND digest=?",
                (scope, candidate.message_id, candidate.origin, digest),
            ).fetchone()
            self.prune(scan_files=False)
            return row["id"]

    def prune(self, *, scan_files=True):
        """Expire metadata and evict oldest assets until within the byte budget."""
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM sightings WHERE created < ?",
                (time.time() - self.retention,),
            )
            self.db.execute(
                "DELETE FROM sightings WHERE id IN (SELECT id FROM sightings ORDER BY created DESC LIMIT -1 OFFSET ?)",
                (self.max_entries,),
            )
            # Oldest last sighting wins. Repeated images in active chats stay useful.
            rows = self.db.execute("""SELECT a.*, MAX(s.created) AS latest FROM assets a
                LEFT JOIN sightings s ON a.digest=s.digest GROUP BY a.digest ORDER BY latest""").fetchall()
            total = sum(row["size"] for row in rows)
            for row in rows:
                path = self.images / (row["digest"] + row["suffix"])
                if (
                    row["latest"] is None
                    or total > self.max_bytes
                    or (scan_files and not path.is_file())
                ):
                    path.unlink(missing_ok=True)
                    self.db.execute(
                        "DELETE FROM sightings WHERE digest=?", (row["digest"],)
                    )
                    self.db.execute(
                        "DELETE FROM assets WHERE digest=?", (row["digest"],)
                    )
                    total -= row["size"]
            if scan_files:
                known = {
                    row[0] + row[1]
                    for row in self.db.execute("SELECT digest, suffix FROM assets")
                }
                for path in self.images.iterdir():
                    if path.is_file() and path.name not in known:
                        path.unlink()

    def recent(self, scope, *, limit=10, sender_id="", message_id="", before=""):
        with self.lock:
            clauses = ["scope=?", "created>=?"]
            params = [scope, time.time() - self.retention]
            for key, value in (("sender_id", sender_id), ("message_id", message_id)):
                if value:
                    clauses.append(key + "=?")
                    params.append(value)
            if before:
                cursor = self.db.execute(
                    "SELECT created, position FROM sightings WHERE scope=? AND id=?",
                    (scope, before),
                ).fetchone()
                if cursor is None:
                    return []
                # Newest message first; images within one message keep their order.
                clauses.append("(created, -position, id) < (?, ?, ?)")
                params.extend([cursor["created"], -cursor["position"], before])
            params.append(max(1, min(int(limit), 20)))
            rows = self.db.execute(
                "SELECT id, message_id, sender_id, sender_name, origin, sent_at, is_bot FROM sightings WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created DESC, position, id DESC LIMIT ?",
                params,
            ).fetchall()
            return [public_row(row) for row in rows]

    def by_message(self, scope, message_ids):
        """Map message IDs to the images each message carried itself, in order."""
        message_ids = list(message_ids)
        with self.lock:
            rows = self.db.execute(
                f"""SELECT id, message_id FROM sightings
                WHERE scope=? AND created>=? AND origin != ?
                AND message_id IN ({",".join("?" * len(message_ids))})
                ORDER BY created, position""",
                (scope, time.time() - self.retention, "引用", *message_ids),
            ).fetchall()
        result = {}
        for row in rows:
            result.setdefault(row["message_id"], []).append(row["id"])
        return result

    def preview(self, scope, image_id):
        """Load only a currently authorized sighting, returning a bounded preview."""
        with self.lock:
            row = self.db.execute(
                """SELECT s.*, a.suffix, a.frames FROM sightings s JOIN assets a ON a.digest=s.digest
                WHERE s.scope=? AND s.id=? AND s.created>=?""",
                (scope, image_id, time.time() - self.retention),
            ).fetchone()
            if row is None:
                raise LookupError(
                    "图片不存在、已过期或不属于当前会话，请重新查询图片列表。"
                )
            try:
                data = (self.images / (row["digest"] + row["suffix"])).read_bytes()
            except FileNotFoundError:
                raise LookupError("缓存文件已被清理，请重新发送图片。") from None
        with Image.open(io.BytesIO(data)) as img:
            img.seek(0)
            # Shrink before creating RGBA/EXIF copies of a potentially 25 MP image.
            img.thumbnail((2048, 2048))
            preview = ImageOps.exif_transpose(img).convert("RGBA")
            background = Image.new("RGB", preview.size, "white")
            background.paste(preview, mask=preview.getchannel("A"))
            output = io.BytesIO()
            background.save(output, format="JPEG", quality=90)
        info = public_row(
            {
                key: row[key]
                for key in (
                    "id",
                    "message_id",
                    "sender_id",
                    "sender_name",
                    "origin",
                    "sent_at",
                    "is_bot",
                )
            }
        )
        info["preview"] = (
            "缩略预览，长边最多 2048px；动图仅首帧"
            if row["frames"] > 1
            else "预览，长边最多 2048px"
        )
        return info, base64.b64encode(output.getvalue()).decode("ascii")

    def close(self):
        with self.lock:
            self.db.close()
