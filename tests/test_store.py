import tempfile
import time
import unittest
from pathlib import Path

from astrbot_plugin_quote_cache.cache_store import CachedMessage, MessageStore


def make_message(
    scope="bot|group:g1",
    astr_id="a1",
    content="hello",
    sender_id="u1",
    sender_name="user",
    timestamp=None,
):
    return CachedMessage(
        scope_key=scope,
        platform_id="bot",
        platform_name="test",
        session_id="g1",
        group_id="g1",
        message_type="GroupMessage",
        astr_message_id=astr_id,
        original_message_id="",
        ref_index="",
        content=content,
        sender_id=sender_id,
        sender_name=sender_name,
        timestamp=timestamp or int(time.time()),
    )


class StoreTests(unittest.TestCase):
    def test_store_alias_and_scope_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=3600)
            message = make_message()
            message_id = store.put(message, [("a1", "astr_message_id")])
            self.assertEqual(store.find(message.scope_key, ["a1"]).content, "hello")
            self.assertIsNone(store.find("bot|group:g2", ["a1"]))
            self.assertEqual(len(store.aliases_for(message_id)), 1)
            store.close()

    def test_substring_multi_keyword_and_sender_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=3600)
            store.put(
                make_message(
                    astr_id="a1",
                    content="周末一起吃火锅吧",
                    sender_id="1001",
                    sender_name="小明",
                ),
                [],
            )
            store.put(
                make_message(astr_id="a2", content="周末改成看电影"),
                [],
            )
            hits = store.search(
                "bot|group:g1", "周末 火锅", sender="小明", limit=5
            )
            self.assertEqual([hit.message.astr_message_id for hit in hits], ["a1"])
            self.assertEqual(hits[0].match_type, "substring")
            store.close()

    def test_fuzzy_search_tolerates_a_typo(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=3600)
            store.put(
                make_message(astr_id="a1", content="今晚一起去吃海底捞吧"),
                [],
            )
            hits = store.search(
                "bot|group:g1",
                "今晚一起去吃海底劳吧",
                limit=5,
                fuzzy_threshold=0.7,
            )
            self.assertEqual(hits[0].message.astr_message_id, "a1")
            self.assertEqual(hits[0].match_type, "fuzzy")
            store.close()

    def test_context_is_chronological_and_scope_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=3600)
            base = int(time.time()) - 10
            ids = []
            for index in range(5):
                ids.append(
                    store.put(
                        make_message(
                            astr_id=f"a{index}",
                            content=f"message-{index}",
                            timestamp=base + index,
                        ),
                        [],
                    )
                )
            context = store.context(
                "bot|group:g1", ids[2], before=1, after=2
            )
            self.assertEqual(
                [message.content for message in context],
                ["message-1", "message-2", "message-3", "message-4"],
            )
            self.assertEqual(store.context("bot|group:g2", ids[2]), [])
            store.close()

    def test_zero_ttl_means_no_time_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=0)
            old = make_message(timestamp=1, content="old but retained")
            store.put(old, [])
            self.assertEqual(store.stats()["messages"], 1)
            self.assertEqual(store.cleanup_expired()[0], 0)
            store.close()

    def test_entry_limit_is_pruned_in_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(
                Path(tmp) / "cache.db", ttl_seconds=3600, max_entries=100
            )
            for index in range(200):
                store.put(
                    make_message(
                        astr_id=f"limit-{index}", content=f"message-{index}"
                    ),
                    [],
                )
            self.assertEqual(store.stats()["messages"], 100)
            self.assertEqual(
                store.search("bot|group:g1", "message-199")[0].message.content,
                "message-199",
            )
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
