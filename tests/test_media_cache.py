import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_quote_cache.media_cache import MediaCache


class MediaCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_materialized_file_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            source.write_bytes(b"first")
            cache = MediaCache(root / "media", enabled=True, max_bytes=1024, timeout=3)
            attachment = {
                "type": "image",
                "filename": "source.png",
                "source": str(source),
            }
            stored = await cache.persist(attachment)
            cached_path = Path(stored["local_path"])
            self.assertEqual(cached_path.read_bytes(), b"first")

            source.write_bytes(b"second")
            stored_again = await cache.persist(stored)
            self.assertEqual(Path(stored_again["local_path"]).read_bytes(), b"first")
            await cache.close()


if __name__ == "__main__":
    unittest.main()
