import importlib.util
import json
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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

    def inbound_event(
        self,
        content="正文",
        *,
        message_id="incoming",
        sender=("user", "用户"),
        components=None,
        own_idx="REFIDX_own==",
        quoted_idx="REFIDX_target==",
    ):
        message = SimpleNamespace(
            message_id=message_id,
            self_id="bot",
            timestamp=None,
            message=components or [{"type": "plain", "text": content}],
            raw_message=SimpleNamespace(
                id=message_id,
                group_openid="g1",
                raw_data={
                    "id": message_id,
                    "message_scene": {
                        "ext": [
                            f"msg_idx={own_idx}",
                            f"ref_msg_idx={quoted_idx}",
                            "auth_token=secret",
                        ]
                    },
                },
            ),
        )
        extras = {}
        return SimpleNamespace(
            message_obj=message,
            unified_msg_origin="qq_official:GroupMessage:g1",
            get_extra=extras.get,
            set_extra=extras.__setitem__,
            get_message_str=lambda: content,
            get_platform_name=lambda: "qq_official",
            get_platform_id=lambda: "bot",
            get_group_id=lambda: "g1",
            get_session_id=lambda: "g1",
            get_sender_id=lambda: sender[0],
            get_sender_name=lambda: sender[1],
            get_message_type=lambda: "GroupMessage",
            get_result=Mock(),
        )

    def group_context(self, enabled, recorder=None):
        """模拟 AstrBot 的配置与内置群聊上下文记录。"""
        self.context.get_config = lambda umo=None: {
            "provider_ltm_settings": {"group_icl_enable": enabled}
        }
        self.context.get_registered_star = lambda name: SimpleNamespace(
            star_cls=SimpleNamespace(group_chat_context=recorder)
        )

    @staticmethod
    def request(*texts):
        return SimpleNamespace(
            contexts=[],
            image_urls=[],
            extra_user_content_parts=[SimpleNamespace(text=t) for t in texts],
        )

    async def test_group_context_lines_get_image_ids_without_new_records(self):
        line = "[张三/14:02:31]:  这题第二问咋做 [Image]"
        recorder = SimpleNamespace(
            raw_records={"qq_official:GroupMessage:g1": deque([line])},
            _record_ids={"qq_official:GroupMessage:g1": deque(["r1"])},
        )
        self.group_context(True, recorder)
        sent = self.inbound_event("这题第二问咋做", message_id="m1")
        sent.set_extra("_group_context_record_id", "r1")
        sent.set_extra(
            "image_cache.capture",
            {"slots": {"own": ["img_a"], "quote": [], "other": []}},
        )
        await self.plugin.capture_message(sent)
        await self.plugin.remember_group_record(sent)

        asking = self.inbound_event("在不在", message_id="m2")
        await self.plugin.capture_message(asking)
        injected = f"<system_reminder>...\n{line}\n--- END CONTEXT ---"
        req = self.request(injected)
        await self.plugin.decorate_request(asking, req)
        texts = [part.text for part in req.extra_user_content_parts]
        self.assertEqual(texts[0], injected.replace("[Image]", "[Image img_a]"))
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[1].startswith("[图片编号说明]"))
        self.assertFalse(any(t.startswith("[群聊记录]") for t in texts))

    async def test_own_transcript_covers_messages_since_last_request(self):
        self.group_context(False)
        self.plugin.images.message_images = AsyncMock(return_value={"m1": ["img_a"]})
        await self.send_receipt()  # 上一轮机器人的回复是首次请求的起点
        await self.plugin.capture_message(
            self.inbound_event(
                message_id="m1",
                sender=("u1", "张三"),
                components=[{"type": "plain", "text": "看这题"}, {"type": "image"}],
                own_idx="REFIDX_m1",
                quoted_idx="",
            )
        )
        await self.plugin.capture_message(
            self.inbound_event(
                message_id="m2",
                sender=("u2", "李四"),
                components=[{"type": "reply"}, {"type": "plain", "text": "我也不会"}],
                own_idx="REFIDX_m2",
                quoted_idx="REFIDX_m1",
            )
        )
        asking = self.inbound_event("在不在", message_id="m3", own_idx="REFIDX_m3")
        await self.plugin.capture_message(asking)
        req = self.request()
        await self.plugin.decorate_request(asking, req)
        (part,) = req.extra_user_content_parts
        lines = part.text.splitlines()
        self.assertEqual(lines[0], "[群聊记录]")
        self.assertTrue(lines[2].endswith("张三：看这题 [图片 img_a]"))
        self.assertIn("李四：[引用 张三 ", lines[3])
        self.assertIn("「看这题 [图片 img_a]」] 我也不会", lines[3])
        self.assertEqual(len(lines), 4)  # 不含机器人回复和当前这条

        # 下一轮只带这一轮之后的消息，超出上限时说明还有未列出的
        self.plugin.group_context_limit = 1
        for index, name in enumerate(("王五", "赵六")):
            await self.plugin.capture_message(
                self.inbound_event(
                    f"第{index}句",
                    message_id=f"n{index}",
                    sender=(name, name),
                    own_idx=f"REFIDX_n{index}",
                )
            )
        again = self.inbound_event("还在吗", message_id="m4", own_idx="REFIDX_m4")
        await self.plugin.capture_message(again)
        req = self.request()
        await self.plugin.decorate_request(again, req)
        text = req.extra_user_content_parts[0].text
        self.assertIn("更早还有 1 条未列出", text)
        self.assertIn("赵六：第1句", text)
        self.assertNotIn("张三", text)
        self.assertNotIn("王五", text)

    async def test_transcript_needs_a_known_starting_point(self):
        self.group_context(False)
        await self.plugin.capture_message(
            self.inbound_event("很久以前", message_id="old")
        )
        asking = self.inbound_event("在不在", message_id="m2", own_idx="REFIDX_m2")
        await self.plugin.capture_message(asking)
        req = self.request()
        await self.plugin.decorate_request(asking, req)
        self.assertEqual(req.extra_user_content_parts, [])
        # 本次请求成为下一轮的起点
        await self.plugin.capture_message(
            self.inbound_event("新消息", message_id="m3", own_idx="REFIDX_m3")
        )
        later = self.inbound_event("还在吗", message_id="m4", own_idx="REFIDX_m4")
        await self.plugin.capture_message(later)
        req = self.request()
        await self.plugin.decorate_request(later, req)
        text = req.extra_user_content_parts[0].text
        self.assertIn("新消息", text)
        self.assertNotIn("很久以前", text)

    async def test_history_tools_follow_allowlist_and_default_limit(self):
        for index in range(3):
            await self.plugin.capture_message(
                self.inbound_event(
                    f"图片缓存 {index}",
                    message_id=f"s{index}",
                    own_idx=f"REFIDX_s{index}",
                )
            )
        asking = self.inbound_event("找一下", message_id="ask", own_idx="REFIDX_ask")
        self.plugin.default_result_limit = 2
        found = json.loads(await self.plugin.search_chat_history(asking, "图片缓存"))
        self.assertEqual(found["count"], 2)
        self.plugin.platform_allowlist = {"telegram"}
        blocked = json.loads(await self.plugin.search_chat_history(asking, "图片缓存"))
        self.assertFalse(blocked["ok"])
        context = json.loads(
            await self.plugin.get_chat_history_context(
                asking, found["results"][0]["record_id"]
            )
        )
        self.assertFalse(context["ok"])

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
        self.assertIsNone(self.plugin._quoted_message(event))
        await self.send_receipt(idx="REFIDX_target==", private=True)
        self.assertIsNone(self.plugin._quoted_message(event))  # other conversation
        await self.send_receipt(idx="REFIDX_target==")
        self.assertTrue(self.plugin._quoted_message(event).is_bot)

    async def test_qq_quote_of_user_message_names_its_author(self):
        await self.plugin.capture_message(self.inbound_event())
        event = self.quoting_event()
        event.message_obj.raw_message.raw_data["message_scene"]["ext"] = [
            "ref_msg_idx=REFIDX_own=="
        ]
        quoted = self.plugin._quoted_message(event)
        self.assertFalse(quoted.is_bot)
        self.assertEqual(
            (quoted.sender_id, quoted.original_message_id), ("user", "incoming")
        )

    async def test_history_results_name_cached_images(self):
        await self.plugin.capture_message(self.inbound_event("看这张图"))
        self.plugin.images.message_images = AsyncMock(
            return_value={"incoming": ["img_1"]}
        )
        asking = self.inbound_event("刚才那张图")
        asking.message_obj.message_id = "asking"
        found = json.loads(await self.plugin.search_chat_history(asking, "这张图"))
        self.assertEqual(found["results"][0]["images"], ["img_1"])
        self.plugin.images.message_images.assert_awaited_with(
            "bot|group:g1", {"incoming"}
        )
        context = json.loads(
            await self.plugin.get_chat_history_context(
                asking, found["results"][0]["record_id"]
            )
        )
        self.assertEqual(context["messages"][0]["images"], ["img_1"])

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
