import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from astrbot_plugin_quote_cache import __path__ as plugin_path


def load_plugin(data_dir):
    """Stub only framework imports; run the real plugin and SQLite store."""
    api = ModuleType("astrbot.api")
    api.logger = Mock()
    events = ModuleType("astrbot.api.event")
    events.AstrMessageEvent = object

    def decorator(*args, **kwargs):
        return lambda func: func

    events.filter = SimpleNamespace(
        event_message_type=decorator,
        on_decorating_result=decorator,
        llm_tool=decorator,
        command=decorator,
        custom_filter=decorator,
        on_llm_request=decorator,
        on_agent_done=decorator,
        CustomFilter=object,
        EventMessageType=SimpleNamespace(ALL="all"),
    )
    provider = ModuleType("astrbot.api.provider")
    provider.ProviderRequest = object
    agent_message = ModuleType("astrbot.core.agent.message")
    agent_message.TextPart = SimpleNamespace
    stars = ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    stars.Star, stars.Context = Star, object
    paths = ModuleType("astrbot.core.utils.astrbot_path")
    paths.get_astrbot_data_path = lambda: data_dir
    modules = {
        "astrbot.api": api,
        "astrbot.api.event": events,
        "astrbot.api.star": stars,
        "astrbot.api.provider": provider,
        "astrbot.core.agent.message": agent_message,
        "astrbot.core.utils.astrbot_path": paths,
    }
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_quote_cache._test_main", Path(plugin_path[0]) / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module.HistorySearchPlugin


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.http = object()
        self.platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="bot", name="qq_official"),
            client=SimpleNamespace(http=self.http),
            appid="appid",
        )
        self.context = SimpleNamespace(
            platform_manager=SimpleNamespace(get_insts=lambda: [self.platform])
        )
        self.plugin = load_plugin(self.tmp.name)(
            self.context, {"auto_cleanup_enabled": False}
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    async def send_receipt(
        self,
        message_id="sent",
        idx="REFIDX_sent==",
        payload=None,
        private=False,
        http=None,
    ):
        route = SimpleNamespace(
            path="/v2/users/{openid}/messages"
            if private
            else "/v2/groups/{group_openid}/messages",
            parameters={"openid" if private else "group_openid": "g1"},
        )
        await self.plugin._cache_qq_send(
            self.http if http is None else http,
            route,
            payload or {"content": "hello", "msg_id": "incoming"},
            {"id": message_id, "ext_info": {"ref_idx": idx}},
        )

    async def test_receipt_maps_both_identifiers_in_correct_scope(self):
        await self.send_receipt()
        hit = self.plugin.store.find("bot|group:g1", ["REFIDX_sent=="])
        self.assertEqual(hit.original_message_id, "sent")
        self.assertTrue(hit.is_bot)
        self.assertIsNone(self.plugin.store.find("bot|group:g1", ["incoming"]))
        self.assertIsNone(self.plugin.store.find("bot|private:g1", ["REFIDX_sent=="]))
        await self.send_receipt("private-sent", "REFIDX_private", private=True)
        self.assertEqual(
            self.plugin.store.find(
                "bot|private:g1", ["REFIDX_private"]
            ).original_message_id,
            "private-sent",
        )

    async def test_split_sends_remain_separate_even_with_same_trigger_and_text(self):
        await self.send_receipt("part-1", "REFIDX_1")
        await self.send_receipt("part-2", "REFIDX_2")
        self.assertEqual(self.plugin.store.stats()["messages"], 2)

    async def test_foreign_client_and_disallowed_platform_are_not_cached(self):
        await self.send_receipt(http=object())
        self.plugin.platform_allowlist = {"other-bot"}
        await self.send_receipt()
        self.assertEqual(self.plugin.store.stats()["messages"], 0)

    async def test_webhook_and_websocket_bot_instances_are_isolated(self):
        second_http = object()
        second = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="webhook", name="qq_official_webhook"),
            client=SimpleNamespace(http=second_http),
            appid="second-appid",
        )
        self.context.platform_manager.get_insts = lambda: [self.platform, second]
        await self.send_receipt(message_id="websocket-sent")
        await self.send_receipt(message_id="webhook-sent", http=second_http)
        self.assertEqual(
            self.plugin.store.find(
                "bot|group:g1", ["REFIDX_sent=="]
            ).original_message_id,
            "websocket-sent",
        )
        self.assertEqual(
            self.plugin.store.find(
                "webhook|group:g1", ["REFIDX_sent=="]
            ).original_message_id,
            "webhook-sent",
        )

    async def test_stream_deltas_deduplicate_and_keep_identity_on_empty_tail(self):
        for index, delta in [(0, "hello "), (1, "world"), (1, "world"), (2, "\n")]:
            await self.send_receipt(
                idx="REFIDX_sent==" if index == 0 else "",
                private=True,
                payload={
                    "markdown": {"content": delta},
                    "stream": {"index": index, "reset": False},
                },
            )
        hit = self.plugin.store.find("bot|private:g1", ["REFIDX_sent=="])
        self.assertEqual(hit.content, "hello world\n")
        self.assertEqual(hit.ref_index, "REFIDX_sent==")
        self.assertEqual(self.plugin.store.stats()["messages"], 1)
        await self.send_receipt(
            private=True,
            payload={
                "content": "reset",
                "stream": {"index": 3, "reset": True},
            },
        )
        self.assertEqual(
            self.plugin.store.find("bot|private:g1", ["sent"]).content, "reset"
        )

    def inbound_event(self, content="正文"):
        message = SimpleNamespace(
            message_id="incoming",
            self_id="bot",
            timestamp=None,
            message=[{"type": "plain", "text": content}],
            raw_message=SimpleNamespace(
                id="incoming",
                group_openid="g1",
                raw_data={
                    "id": "incoming",
                    "message_scene": {
                        "ext": [
                            "msg_idx=REFIDX_own==",
                            "ref_msg_idx=REFIDX_target==",
                            "auth_token=secret",
                        ]
                    },
                },
            ),
        )
        return SimpleNamespace(
            message_obj=message,
            get_message_str=lambda: content,
            get_platform_name=lambda: "qq_official",
            get_platform_id=lambda: "bot",
            get_group_id=lambda: "g1",
            get_session_id=lambda: "g1",
            get_sender_id=lambda: "user",
            get_sender_name=lambda: "用户",
            get_message_type=lambda: "GroupMessage",
            get_result=Mock(),
        )

    async def test_inbound_stores_own_indices_and_never_aliases_quoted_target(self):
        event = self.inbound_event()
        await self.plugin.capture_message(event)
        hit = self.plugin.store.find("bot|group:g1", ["REFIDX_own=="])
        self.assertEqual(hit.original_message_id, "incoming")
        self.assertEqual(hit.raw_meta["ref_msg_idx"], "REFIDX_target==")
        self.assertNotIn("secret", repr(hit))
        self.assertIsNone(self.plugin.store.find("bot|group:g1", ["REFIDX_target=="]))
        await self.plugin.capture_bot_response(event)
        event.get_result.assert_not_called()
        self.assertEqual(self.plugin.store.stats()["messages"], 1)

    async def test_identifier_only_message_is_cached(self):
        await self.plugin.capture_message(self.inbound_event(content=""))
        self.assertIsNotNone(self.plugin.store.find("bot|group:g1", ["REFIDX_own=="]))

    def quoting_event(self):
        return self.inbound_event()

    async def test_qq_quote_of_bot_receipt_is_recognized(self):
        event = self.quoting_event()
        self.assertFalse(self.plugin._quotes_bot(event))
        await self.send_receipt(idx="REFIDX_target==", private=True)
        self.assertFalse(self.plugin._quotes_bot(event))  # other conversation
        await self.send_receipt(idx="REFIDX_target==")
        self.assertTrue(self.plugin._quotes_bot(event))

    async def test_qq_quote_of_user_message_is_not_the_bot(self):
        await self.plugin.capture_message(self.inbound_event())
        event = self.quoting_event()
        event.message_obj.raw_message.raw_data["message_scene"]["ext"] = [
            "ref_msg_idx=REFIDX_own=="
        ]
        self.assertFalse(self.plugin._quotes_bot(event))

    async def test_image_cache_follows_allowlist_not_history_switch(self):
        event = self.inbound_event()
        self.plugin.images.cache = object()
        self.plugin.enabled = False
        self.assertTrue(self.plugin.images.active(event))
        self.plugin.platform_allowlist = {"telegram"}
        self.assertFalse(self.plugin.images.active(event))
        self.plugin.images.cache = None

    def test_image_tool_docstrings_use_supported_types(self):
        import docstring_parser

        plugin_class = type(self.plugin)
        view = docstring_parser.parse(plugin_class.image_cache_view.__doc__)
        listing = docstring_parser.parse(plugin_class.image_cache_list.__doc__)
        self.assertEqual(
            [(p.arg_name, p.type_name) for p in view.params],
            [("image_ids", "array[string]")],
        )
        self.assertEqual(
            {p.arg_name for p in listing.params},
            {"limit", "sender_id", "message_id", "before"},
        )
        # AstrBot only accepts these JSON schema names in tool docstrings.
        supported = {"string", "number", "object", "array", "boolean"}
        for param in view.params + listing.params:
            outer, _, inner = param.type_name.rstrip("]").partition("[")
            self.assertLessEqual({outer, inner or outer}, supported)
