import asyncio
import base64
import io
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from astrbot_plugin_quote_cache.image_extraction import Candidate
from astrbot_plugin_quote_cache.image_store import (
    ImageStore,
    PublicResolver,
    read_source,
    validate_url,
)
from PIL import Image

CANDIDATE = Candidate("unused", "m1", "当前消息", "u1", "张三", 100)


@pytest.fixture
def cache(tmp_path):
    store = ImageStore(tmp_path)
    yield store
    store.close()


def test_scope_dedup_preview_and_restart(cache, png_bytes):
    first = cache.store("bot1/group1", CANDIDATE, png_bytes)
    assert first == cache.store("bot1/group1", CANDIDATE, png_bytes)
    second = cache.store("bot2/group1", CANDIDATE, png_bytes)
    assert first != second
    assert len(list(cache.images.iterdir())) == 1
    with pytest.raises(LookupError):
        cache.preview("bot1/group1", second)
    with pytest.raises(LookupError):
        cache.preview("bot2/group2", second)
    info, encoded = cache.preview("bot1/group1", first)
    assert info["id"] == first
    assert Image.open(io.BytesIO(base64.b64decode(encoded))).size == (32, 24)
    reopened = ImageStore(cache.root)
    assert reopened.recent("bot1/group1")[0]["id"] == first
    reopened.close()


def test_expiration_cannot_be_bypassed_by_known_id(cache, png_bytes, monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_quote_cache.image_store.time.time", lambda: 1_000_000
    )
    image_id = cache.store("scope", CANDIDATE, png_bytes)
    monkeypatch.setattr(
        "astrbot_plugin_quote_cache.image_store.time.time", lambda: 2_000_000
    )
    assert cache.recent("scope") == []
    with pytest.raises(LookupError):
        cache.preview("scope", image_id)
    cache.prune()
    assert list(cache.images.iterdir()) == []


def test_byte_budget_evicts_oldest_asset(tmp_path, png_bytes, monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(output, format="PNG")
    other = output.getvalue()
    cache = ImageStore(tmp_path, max_bytes=max(len(png_bytes), len(other)))
    monkeypatch.setattr(
        "astrbot_plugin_quote_cache.image_store.time.time", lambda: 1000
    )
    first = cache.store("scope", CANDIDATE, png_bytes)
    monkeypatch.setattr(
        "astrbot_plugin_quote_cache.image_store.time.time", lambda: 1001
    )
    second = cache.store("scope", replace(CANDIDATE, message_id="m2"), other)
    with pytest.raises(LookupError):
        cache.preview("scope", first)
    assert cache.recent("scope")[0]["id"] == second
    assert (
        sum(path.stat().st_size for path in cache.images.iterdir()) <= cache.max_bytes
    )
    cache.close()


def test_invalid_image_and_missing_file(cache, png_bytes):
    with pytest.raises(OSError):
        cache.store("scope", CANDIDATE, b"<html>not an image</html>")
    image_id = cache.store("scope", CANDIDATE, png_bytes)
    next(cache.images.iterdir()).unlink()
    with pytest.raises(LookupError, match="清理"):
        cache.preview("scope", image_id)
    cache.prune()
    assert cache.recent("scope") == []


def test_concurrent_admission_and_filters(cache, png_bytes):
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(
            pool.map(lambda _: cache.store("scope", CANDIDATE, png_bytes), range(12))
        )
    assert len(set(ids)) == 1
    assert cache.recent("scope", sender_id="other") == []
    assert cache.recent("scope", message_id="m1")[0]["id"] == ids[0]
    assert cache.recent("scope", before="not-owned") == []


def test_pagination_with_equal_timestamps(cache, png_bytes, monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_quote_cache.image_store.time.time", lambda: 1000
    )
    ids = {
        cache.store("scope", replace(CANDIDATE, message_id=str(i)), png_bytes)
        for i in range(5)
    }
    first = cache.recent("scope", limit=2)
    second = cache.recent("scope", limit=3, before=first[-1]["id"])
    assert {row["id"] for row in first + second} == ids


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "http://[::1]/a",
        "http://169.254.169.254/a",
        "http://user:password@example.org/a",
        "file:///a",
        "https://example.org:8080/a",
        "http://[::ffff:127.0.0.1]/a",
        "http://224.0.0.1/a",
        "http://[ff02::1]/a",
        "http://[fec0::1]/a",
    ],
)
def test_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


async def test_dns_resolution_checks_actual_connector_addresses(monkeypatch):
    resolver = PublicResolver()

    async def unsafe(*args):
        return [{"host": "127.0.0.1"}]

    monkeypatch.setattr(resolver.resolver, "resolve", unsafe)
    with pytest.raises(ValueError):
        await resolver.resolve("gchat.qpic.cn", 443)
    await resolver.close()


class Response:
    def __init__(self, data=b"", status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.content_length = None
        self.content = self
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    async def iter_chunked(self, size):
        yield self.data


async def test_redirect_to_private_address_is_not_requested():
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(status=302, headers={"Location": "http://127.0.0.1/secret"})

    with pytest.raises(ValueError):
        await read_source(
            NS(get=get), "https://gchat.qpic.cn/a", local_allowed=False, max_bytes=1024
        )
    assert len(calls) == 1


async def test_stream_limit_and_local_sources(tmp_path, png_bytes):
    session = NS(get=lambda *args, **kwargs: Response(b"a" * 11))
    with pytest.raises(ValueError, match="size"):
        await read_source(
            session, "https://gchat.qpic.cn/a", local_allowed=False, max_bytes=10
        )
    path = tmp_path / "图.png"
    path.write_bytes(png_bytes)
    assert (
        await read_source(None, path.as_uri(), local_allowed=True, max_bytes=1024)
        == png_bytes
    )
    with pytest.raises(ValueError):
        await read_source(None, str(path), local_allowed=False, max_bytes=1024)
    with pytest.raises(ValueError):
        await read_source(
            None,
            "base64://" + base64.b64encode(png_bytes).decode(),
            local_allowed=True,
            max_bytes=10,
        )


async def test_total_download_deadline():
    class SlowResponse(Response):
        async def iter_chunked(self, size):
            await asyncio.sleep(10)
            yield b"x"

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            read_source(
                NS(get=lambda *args, **kwargs: SlowResponse()),
                "https://gchat.qpic.cn/a",
                local_allowed=False,
                max_bytes=10,
            ),
            timeout=0.01,
        )


async def test_http_compression_is_disabled_before_body_read():
    def get(url, **kwargs):
        assert kwargs["auto_decompress"] is False
        assert kwargs["headers"]["Accept-Encoding"] == "identity"
        return Response(headers={"Content-Encoding": "gzip"})

    with pytest.raises(ValueError, match="compressed"):
        await read_source(
            NS(get=get), "https://gchat.qpic.cn/a", local_allowed=False, max_bytes=10
        )


def test_admission_does_not_scan_directory_but_maintenance_does(cache, png_bytes):
    orphan = cache.images / "orphan.part"
    orphan.write_bytes(b"interrupted write")
    cache.store("scope", CANDIDATE, png_bytes)
    assert orphan.exists()
    cache.prune()
    assert not orphan.exists()


def test_large_preview_is_resized_before_rgba_copy(cache, monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (4000, 3000), "blue").save(output, format="JPEG")
    image_id = cache.store("scope", CANDIDATE, output.getvalue())
    original = Image.Image.convert

    def convert(image, mode=None, *args, **kwargs):
        if mode == "RGBA":
            assert max(image.size) <= 2048
        return original(image, mode, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", convert)
    _, encoded = cache.preview("scope", image_id)
    assert Image.open(io.BytesIO(base64.b64decode(encoded))).size == (2048, 1536)


def test_bot_attribution_is_stored_and_exposed_only_when_true(cache, png_bytes):
    user = cache.store("s", CANDIDATE, png_bytes)
    bot = cache.store("s", replace(CANDIDATE, message_id="m2", is_bot=True), png_bytes)
    flags = {row["id"]: row.get("sender_is_bot") for row in cache.recent("s")}
    assert flags == {user: None, bot: True}
    assert cache.preview("s", bot)[0]["sender_is_bot"] is True
    assert "sender_is_bot" not in cache.preview("s", user)[0]


def test_listing_follows_arrival_and_position(cache, png_bytes):
    now = time.time()
    later = cache.store("s", replace(CANDIDATE, message_id="m2"), png_bytes, seen=now)
    second = cache.store("s", CANDIDATE, png_bytes, seen=now - 1, position=1)
    first = cache.store("s", replace(CANDIDATE, origin="引用"), png_bytes, seen=now - 1)
    assert [row["id"] for row in cache.recent("s")] == [later, first, second]
    assert [row["id"] for row in cache.recent("s", before=first)] == [second]


def test_history_lookup_returns_only_own_images(cache, png_bytes):
    own = cache.store("s", CANDIDATE, png_bytes)
    cache.store("s", replace(CANDIDATE, origin="引用"), png_bytes)
    assert cache.by_message("s", ["m1", "m2"]) == {"m1": [own]}
    assert cache.by_message("other", ["m1"]) == {}


def test_existing_index_gains_position_column(tmp_path, png_bytes):
    import sqlite3

    db = sqlite3.connect(tmp_path / "index.sqlite3")
    db.execute("""CREATE TABLE sightings (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, message_id TEXT NOT NULL,
        sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
        origin TEXT NOT NULL, sent_at REAL NOT NULL, created REAL NOT NULL,
        digest TEXT NOT NULL, is_bot INTEGER NOT NULL DEFAULT 0,
        UNIQUE(scope, message_id, origin, digest))""")
    db.close()
    cache = ImageStore(tmp_path)
    image_id = cache.store("s", CANDIDATE, png_bytes, position=2)
    assert cache.recent("s")[0]["id"] == image_id
    cache.close()
