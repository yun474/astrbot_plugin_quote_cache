import tempfile
import time
import unittest
from pathlib import Path

from astrbot_plugin_quote_cache.cache_store import CachedMessage, MessageStore


def make_message(scope="bot|group:g1", astr_id="a1"):
    return CachedMessage(
        scope_key=scope,
        platform_id="bot",
        platform_name="qq_official",
        session_id="g1",
        group_id="g1",
        message_type="GroupMessage",
        astr_message_id=astr_id,
        original_message_id="qq1",
        ref_index="REFIDX-1",
        content="hello",
        sender_id="u1",
        sender_name="user",
        timestamp=int(time.time()),
    )


class StoreTests(unittest.TestCase):
    def test_store_alias_and_scope_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=3600)
            message = make_message()
            message_id = store.put(
                message,
                [("a1", "astr_message_id"), ("REFIDX-1", "msg_idx")],
            )
            self.assertEqual(
                store.find(message.scope_key, ["REFIDX-1"]).content, "hello"
            )
            self.assertIsNone(store.find("bot|group:g2", ["REFIDX-1"]))
            self.assertEqual(len(store.aliases_for(message_id)), 2)
            store.close()

    def test_store_update_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=3600)
            message = make_message()
            store.put(message, [("a1", "astr_message_id")])
            message.content = "updated"
            store.put(message, [("new-alias", "raw_id")])
            self.assertEqual(
                store.find(message.scope_key, ["new-alias"]).content, "updated"
            )
            removed, _ = store.clear(message.scope_key)
            self.assertEqual(removed, 1)
            store.close()

    def test_close_is_idempotent_and_exposes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=3600)
            self.assertFalse(store.closed)
            store.close()
            self.assertTrue(store.closed)
            store.close()


if __name__ == "__main__":
    unittest.main()
