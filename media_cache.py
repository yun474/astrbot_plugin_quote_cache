from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import mimetypes
import os
import shutil
import socket
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None


class MediaCache:
    def __init__(self, root: Path, enabled: bool, max_bytes: int, timeout: int):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.enabled = enabled
        self.max_bytes = max(int(max_bytes), 1024)
        self.timeout = max(int(timeout), 3)
        self._session = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def persist_all(self, attachments: list[dict]) -> list[dict]:
        if not self.enabled or not attachments:
            return attachments
        return list(await asyncio.gather(*(self.persist(dict(x)) for x in attachments)))

    async def persist(self, attachment: dict) -> dict:
        cached_path = str(attachment.get("local_path") or "")
        if cached_path:
            try:
                if Path(cached_path).is_file():
                    return attachment
            except OSError:
                pass
        source = str(attachment.get("source") or attachment.get("path") or attachment.get("url") or attachment.get("file") or "")
        if not source:
            return attachment
        try:
            if source.startswith("base64://"):
                data = base64.b64decode(source[9:], validate=True)
                if len(data) > self.max_bytes:
                    return attachment
                target = self._target(attachment, source)
                await asyncio.to_thread(target.write_bytes, data)
            elif source.startswith("data:") and ";base64," in source[:200]:
                header, encoded = source.split(",", 1)
                data = base64.b64decode(encoded, validate=True)
                if len(data) > self.max_bytes:
                    return attachment
                if not attachment.get("content_type"):
                    attachment["content_type"] = header[5:].split(";", 1)[0]
                target = self._target(attachment, source)
                await asyncio.to_thread(target.write_bytes, data)
            elif source.startswith(("http://", "https://")):
                target = self._target(attachment, source)
                await self._download_public(source, target)
            else:
                local = self._local_path(source)
                if not local or not local.is_file() or local.stat().st_size > self.max_bytes:
                    return attachment
                target = self._target(attachment, source)
                await asyncio.to_thread(shutil.copy2, local, target)
            attachment["local_path"] = str(target.resolve())
            attachment["cached_size"] = target.stat().st_size
        except Exception as exc:  # media caching is best-effort
            attachment["cache_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return attachment

    def _local_path(self, source: str) -> Path | None:
        if source.startswith("file://"):
            parsed = urlparse(source)
            path = unquote(parsed.path)
            if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
                path = path[1:]
            return Path(path)
        try:
            return Path(source)
        except (TypeError, ValueError, OSError):
            return None

    def _target(self, attachment: dict, source: str) -> Path:
        name = str(attachment.get("filename") or "").replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(name).suffix[:12]
        if not suffix:
            suffix = Path(urlparse(source).path).suffix[:12]
        if not suffix:
            suffix = mimetypes.guess_extension(str(attachment.get("content_type") or "")) or ".bin"
        digest = hashlib.sha256(source.encode("utf-8", "ignore")).hexdigest()[:24]
        target = self.root / f"{digest}{suffix}"
        counter = 1
        while target.exists() and target.stat().st_size == 0:
            target = self.root / f"{digest}-{counter}{suffix}"
            counter += 1
        return target

    async def _is_public_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        addresses = {item[4][0].split("%", 1)[0] for item in infos}
        if not addresses:
            return False
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                return False
        return True

    async def _download_public(self, url: str, target: Path) -> None:
        if aiohttp is None:
            raise RuntimeError("aiohttp unavailable")
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout, trust_env=False)
        current = url
        for _ in range(4):
            if not await self._is_public_url(current):
                raise ValueError("blocked non-public attachment URL")
            async with self._session.get(current, allow_redirects=False) as resp:
                if resp.status in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("Location")
                    if not location:
                        raise ValueError("redirect without Location")
                    current = urljoin(current, location)
                    continue
                resp.raise_for_status()
                declared = int(resp.headers.get("Content-Length", "0") or 0)
                if declared > self.max_bytes:
                    raise ValueError("attachment exceeds size limit")
                total = 0
                with target.open("wb") as handle:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > self.max_bytes:
                            handle.close()
                            target.unlink(missing_ok=True)
                            raise ValueError("attachment exceeds size limit")
                        handle.write(chunk)
                return
        raise ValueError("too many redirects")

    def remove_paths(self, paths: list[str]) -> int:
        removed = 0
        root = self.root.resolve()
        for raw in set(paths):
            try:
                target = Path(raw).resolve()
                if target.parent == root and target.is_file():
                    target.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
