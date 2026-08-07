import sqlite3
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
    group_name="测试群",
):
    return CachedMessage(
        scope_key=scope,
        platform_id="bot",
        platform_name="test",
        session_id="g1",
        group_id="g1",
        group_name=group_name,
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
    def test_group_name_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=3600)
            store.put(make_message(group_name="云云测试群"), [])
            hit = store.search("bot|group:g1", "hello")[0]
            self.assertEqual(hit.message.group_name, "云云测试群")
            store.close()

    def test_existing_database_is_migrated_with_group_name_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cache.db"
            db = sqlite3.connect(db_path)
            db.executescript(
                """
                CREATE TABLE messages (
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
                CREATE TABLE aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    scope_key TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    alias_kind TEXT NOT NULL DEFAULT 'unknown',
                    UNIQUE(message_id, scope_key, alias)
                );
                """
            )
            db.commit()
            db.close()

            store = MessageStore(db_path, ttl_seconds=3600)
            store.put(make_message(group_name="迁移后的群"), [])
            hit = store.search("bot|group:g1", "hello")[0]
            self.assertEqual(hit.message.group_name, "迁移后的群")
            store.close()

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
            hits = store.search("bot|group:g1", "周末 火锅", sender="小明", limit=5)
            self.assertEqual([hit.message.astr_message_id for hit in hits], ["a1"])
            self.assertEqual(hits[0].match_type, "substring")
            store.close()

    def test_global_search_can_cross_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=3600)
            store.put(
                make_message(
                    scope="bot|group:g1",
                    astr_id="g1-message",
                    content="只有一群有的关键词",
                ),
                [],
            )
            other = make_message(
                scope="bot|group:g2",
                astr_id="g2-message",
                content="只有二群有的关键词",
            )
            other.session_id = "g2"
            other.group_id = "g2"
            store.put(other, [])

            scoped_hits = store.search("bot|group:g1", "二群", limit=5)
            global_hits = store.search(None, "二群", limit=5)

            self.assertEqual(scoped_hits, [])
            self.assertEqual(
                [hit.message.astr_message_id for hit in global_hits],
                ["g2-message"],
            )
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
            context = store.context("bot|group:g1", ids[2], before=1, after=2)
            self.assertEqual(
                [message.content for message in context],
                ["message-1", "message-2", "message-3", "message-4"],
            )
            self.assertEqual(store.context("bot|group:g2", ids[2]), [])
            store.close()

    def test_global_context_stays_in_anchor_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(Path(tmp) / "cache.db", ttl_seconds=3600)
            base = int(time.time()) - 10
            first = make_message(
                scope="bot|group:g1",
                astr_id="g1-first",
                content="g1-first",
                timestamp=base,
            )
            anchor = make_message(
                scope="bot|group:g1",
                astr_id="g1-anchor",
                content="g1-anchor",
                timestamp=base + 2,
            )
            foreign = make_message(
                scope="bot|group:g2",
                astr_id="g2-between",
                content="g2-between",
                timestamp=base + 1,
            )
            foreign.session_id = "g2"
            foreign.group_id = "g2"
            store.put(first, [])
            store.put(foreign, [])
            anchor_id = store.put(anchor, [])

            context = store.context(None, anchor_id, before=3, after=3)

            self.assertEqual(
                [message.content for message in context],
                ["g1-first", "g1-anchor"],
            )
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
                    make_message(astr_id=f"limit-{index}", content=f"message-{index}"),
                    [],
                )
            self.assertEqual(store.stats()["messages"], 100)
            self.assertEqual(
                store.search("bot|group:g1", "message-199")[0].message.content,
                "message-199",
            )
            store.close()

    def test_zero_entry_limit_does_not_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(
                Path(tmp) / "cache.db",
                ttl_seconds=3600,
                max_entries=0,
            )
            for index in range(150):
                store.put(
                    make_message(
                        astr_id=f"unlimited-{index}",
                        content=f"message-{index}",
                    ),
                    [],
                )
            self.assertEqual(store.stats()["messages"], 150)
            store.close()

    def test_per_group_limit_only_prunes_that_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(
                Path(tmp) / "cache.db",
                ttl_seconds=3600,
                max_entries=0,
                max_entries_per_group=2,
            )
            for index in range(3):
                store.put(
                    make_message(
                        astr_id=f"g1-{index}",
                        content=f"g1-message-{index}",
                    ),
                    [],
                )
            other = make_message(
                scope="bot|group:g2",
                astr_id="g2-0",
                content="g2-message",
            )
            other.session_id = "g2"
            other.group_id = "g2"
            store.put(other, [])

            self.assertEqual(store.stats("bot|group:g1")["messages"], 2)
            self.assertEqual(store.stats("bot|group:g2")["messages"], 1)
            self.assertEqual(store.stats()["messages"], 3)
            self.assertEqual(
                store.search("bot|group:g1", "g1-message-0", fuzzy_threshold=0.95),
                [],
            )
            store.close()

    def test_zero_per_group_limit_does_not_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(
                Path(tmp) / "cache.db",
                ttl_seconds=3600,
                max_entries=0,
                max_entries_per_group=0,
            )
            for index in range(3):
                store.put(
                    make_message(
                        astr_id=f"g1-unlimited-{index}",
                        content=f"message-{index}",
                    ),
                    [],
                )
            self.assertEqual(store.stats("bot|group:g1")["messages"], 3)
            store.close()

    def test_per_group_limit_prunes_existing_database_on_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cache.db"
            store = MessageStore(
                db_path,
                ttl_seconds=3600,
                max_entries=0,
                max_entries_per_group=0,
            )
            for index in range(3):
                store.put(
                    make_message(
                        astr_id=f"existing-{index}",
                        content=f"message-{index}",
                    ),
                    [],
                )
            store.close()

            reopened = MessageStore(
                db_path,
                ttl_seconds=3600,
                max_entries=0,
                max_entries_per_group=2,
            )
            self.assertEqual(reopened.stats("bot|group:g1")["messages"], 2)
            reopened.close()

    def test_per_group_limit_does_not_prune_private_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(
                Path(tmp) / "cache.db",
                ttl_seconds=3600,
                max_entries=0,
                max_entries_per_group=1,
            )
            for index in range(2):
                private = make_message(
                    scope="bot|private:u1",
                    astr_id=f"private-{index}",
                    content=f"private-message-{index}",
                )
                private.session_id = "u1"
                private.group_id = ""
                private.message_type = "FriendMessage"
                store.put(private, [])

            self.assertEqual(store.stats("bot|private:u1")["messages"], 2)
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
