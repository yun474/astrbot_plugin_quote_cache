import asyncio
import io
import json
import threading
from types import SimpleNamespace as NS

import pytest
from mcp.types import ImageContent
from PIL import Image


class Event:
    def __init__(
        self,
        chain=(),
        raw=None,
        *,
        platform="qq_official",
        platform_id="bot1",
        scope="qq_official:group:g1",
        message_id="m1",
        group_id="",
        self_id="bot",
        quotes_bot=False,
    ):
        self.platform = platform
        self.platform_id = platform_id
        self.unified_msg_origin = scope
        self.group_id = group_id
        self.self_id = self_id
        self.quotes_bot = quotes_bot
        self.message_obj = NS(
            message=list(chain),
            raw_message=NS(raw_data=raw or {}),
            message_id=message_id,
            timestamp=100,
        )
        self.extras = {}

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return self.platform_id

    def get_messages(self):
        return self.message_obj.message

    def get_group_id(self):
        return self.group_id

    def get_self_id(self):
        return self.self_id

    def get_sender_id(self):
        return "u1"

    def get_sender_name(self):
        return "张三"

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value


class Context:
    def __init__(self, modalities=()):
        self.provider = NS(provider_config={"modalities": list(modalities)})

    def get_using_provider(self, umo=None):
        return self.provider

    def get_provider_by_id(self, provider_id):
        return self.provider


def scope_of(event):
    platform, group = event.get_platform_id(), event.get_group_id()
    if group:
        return f"{platform}|group:{group}"
    return f"{platform}|private:{event.unified_msg_origin}"


def make_service(module, root, settings=None):
    # main.py decides whether a quote targets the bot; tests state it per event.
    return module.ImageCacheService(
        Context(),
        settings or {},
        root,
        scope_of=scope_of,
        allowed=lambda event: True,
        quotes_bot=lambda event: event.quotes_bot,
    )


@pytest.fixture
async def plugin(image_module, tmp_path):
    instance = make_service(image_module, tmp_path / "image_cache")
    await instance.initialize()
    yield instance
    await instance.terminate()


async def test_capture_without_wakeup_and_repeated_view(
    plugin, tmp_path, png_bytes, image_module
):
    path = tmp_path / "image.png"
    path.write_bytes(png_bytes)
    event = Event([NS(type="Image", file=str(path))])
    await plugin.capture(event)
    state = event.get_extra(image_module.EVENT_KEY)
    assert len(state["ids"]) == 1 and state["failed"] == 0
    await plugin.capture(event)
    listed = json.loads(await plugin.list_images(event))
    assert len(listed["images"]) == 1
    # Original framework temporary file may disappear; plugin retains its own bytes.
    path.unlink()
    for _ in range(2):
        result = await plugin.view_images(event, state["ids"])
        assert not result.isError
        assert any(isinstance(item, ImageContent) for item in result.content)


async def test_qq_forward_url_to_cache_to_model(
    plugin, image_module, png_bytes, monkeypatch
):
    seen = []

    async def download(session, ref, **kwargs):
        seen.append(ref)
        assert not kwargs["local_allowed"]
        return png_bytes

    monkeypatch.setattr(image_module, "read_source", download)
    event = Event(
        raw={
            "msg_elements": [
                {"id": "forward-child", "content": "https://gchat.qpic.cn/a?sig=xyz"}
            ]
        }
    )
    await plugin.capture(event)
    image_id = event.get_extra(image_module.EVENT_KEY)["ids"][0]
    result = await plugin.view_images(event, [image_id])
    assert seen == ["https://gchat.qpic.cn/a?sig=xyz"]
    assert json.loads(result.content[0].text)["message_id"] == "forward-child"
    assert isinstance(result.content[1], ImageContent)
    assert "xyz" not in await plugin.list_images(event)


async def test_tools_enforce_platform_and_conversation(plugin, png_bytes):
    from astrbot_plugin_quote_cache.image_extraction import Candidate

    event = Event()
    image_id = plugin.cache.store(
        plugin.scope_of(event), Candidate("unused", "m1", "当前消息"), png_bytes
    )
    for other in (
        Event(scope="other"),
        Event(platform_id="bot2"),
        Event(platform="telegram"),
    ):
        assert (await plugin.view_images(other, [image_id])).isError
        assert image_id not in await plugin.list_images(other)
    assert (await plugin.view_images(event, [image_id] * 4)).isError
    assert (await plugin.view_images(event, "not-a-list")).isError


async def test_request_only_adds_metadata_and_retains_current_images(
    plugin, image_module, png_bytes
):
    from astrbot_plugin_quote_cache.image_extraction import Candidate

    event = Event()
    plugin.cache.store(
        plugin.scope_of(event), Candidate("unused", "m1", "当前消息"), png_bytes
    )
    previous = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": image_module.VIEW_MARKER + "old.jpg']"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,OWN"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,OTHER"},
                },
            ],
        }
    ]
    request = NS(
        contexts=previous,
        extra_user_content_parts=[],
        image_urls=["current.png"],
        prompt="看看刚才那张",
    )
    await plugin.add_index(event, request)
    assert request.image_urls == ["current.png"]
    assert request.prompt == "看看刚才那张"
    assert all(part.type == "text" for part in request.extra_user_content_parts)
    assert request.extra_user_content_parts[0].text.startswith(
        image_module.INDEX_PREFIX
    )
    assert "OWN" not in str(request.contexts) and "OTHER" in str(request.contexts)
    assert "OWN" in str(previous)  # No in-place mutation of shared history.


async def test_agent_done_drops_only_owned_visuals(plugin, image_module):
    models = image_module.test_models

    def image(label):
        if hasattr(models, "ImageURLPart"):
            return models.ImageURLPart(
                image_url=models.ImageURLPart.ImageURL(url=label)
            )
        return NS(type="image_url", image_url=NS(url=label))

    parts = [
        models.TextPart(text="用户问题"),
        models.TextPart(text=image_module.INDEX_PREFIX + "index"),
        models.TextPart(text=image_module.VIEW_MARKER + "cached.jpg']"),
        image("OWN"),
        models.TextPart(text="[Image from tool 'other', path='x']"),
        image("OTHER"),
    ]
    if hasattr(models, "Message"):
        message = models.Message(role="user", content=parts)
    else:
        message = NS(role="user", content=parts)
    await plugin.drop_previews(Event(), NS(messages=[message]))
    urls = [part.image_url.url for part in message.content if part.type == "image_url"]
    assert urls == ["OTHER"]
    assert not any(
        part.type == "text" and part.text.startswith(image_module.INDEX_PREFIX)
        for part in message.content
    )


async def test_download_failure_is_visible_without_leaking_signed_url(
    plugin, image_module, monkeypatch, caplog
):
    async def failed(*args, **kwargs):
        raise ValueError("https://gchat.qpic.cn/a?secret=value")

    monkeypatch.setattr(image_module, "read_source", failed)
    event = Event(raw={"content": "https://gchat.qpic.cn/a?secret=value"})
    request = NS(contexts=[], extra_user_content_parts=[], image_urls=[])
    await plugin.add_index(event, request)
    assert '"本条缓存失败数": 1' in request.extra_user_content_parts[0].text
    assert "secret=value" not in caplog.text


async def test_animated_preview_is_explicit(plugin, image_module):
    from astrbot_plugin_quote_cache.image_extraction import Candidate

    output = io.BytesIO()
    first = Image.new("RGB", (20, 20), "red")
    second = Image.new("RGB", (20, 20), "blue")
    first.save(output, format="GIF", save_all=True, append_images=[second])
    event = Event()
    image_id = plugin.cache.store(
        plugin.scope_of(event), Candidate("unused", "m1", "当前消息"), output.getvalue()
    )
    result = await plugin.view_images(event, [image_id])
    assert "首帧" in result.content[0].text


async def test_webhook_forward_is_cached(plugin, image_module, png_bytes, monkeypatch):
    async def download(*args, **kwargs):
        return png_bytes

    monkeypatch.setattr(image_module, "read_source", download)
    event = Event(
        raw={"msg_elements": [{"content": "https://gchat.qpic.cn/a"}]},
        platform="qq_official_webhook",
    )
    await plugin.capture(event)
    assert len(event.get_extra(image_module.EVENT_KEY)["ids"]) == 1
    assert image_module.has_image_candidate(event)
    assert not image_module.has_image_candidate(Event(platform="telegram"))


def test_capture_filter_leaves_plain_chat_asleep(image_module):
    has_image = image_module.has_image_candidate
    assert not has_image(Event([NS(type="Plain", text="hi")]))
    assert has_image(Event([NS(type="Image", file="a.png")]))
    assert has_image(Event([NS(type="Reply", chain=[NS(type="Image", file="a.png")])]))


async def test_capture_fault_does_not_escape_to_pipeline(
    plugin, image_module, monkeypatch
):
    def malformed(*args, **kwargs):
        raise ValueError("payload with secrets")

    monkeypatch.setattr(image_module, "collect_images", malformed)
    event = Event()
    await plugin.capture(event)
    assert event.get_extra(image_module.EVENT_KEY) == {"ids": [], "failed": 1}


async def test_busy_groups_have_bounded_pending_downloads(
    plugin, image_module, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def download(*args, **kwargs):
        entered.set()
        await release.wait()
        raise ValueError("test failure")

    monkeypatch.setattr(image_module, "read_source", download)
    raw = {"content": " ".join(f"https://gchat.qpic.cn/{i}" for i in range(20))}
    events = [Event(raw=raw, message_id=str(i)) for i in range(5)]
    tasks = [asyncio.create_task(plugin.capture(event)) for event in events]
    await entered.wait()
    assert len(plugin.download_tasks) == 64
    release.set()
    await asyncio.gather(*tasks)
    assert not plugin.download_tasks
    assert (
        sum(event.get_extra(image_module.EVENT_KEY)["failed"] for event in events)
        == 100
    )


async def test_shutdown_drains_worker_before_closing_database(
    image_module, monkeypatch, tmp_path
):
    plugin = make_service(image_module, tmp_path)
    await plugin.initialize()
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    close = plugin.cache.close

    def work():
        started.set()
        release.wait(5)
        completed.set()

    def checked_close():
        assert completed.is_set()
        close()

    monkeypatch.setattr(plugin.cache, "close", checked_close)
    task = asyncio.create_task(plugin._run_io(work))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    terminating = asyncio.create_task(plugin.terminate())
    await asyncio.sleep(0)
    assert not terminating.done()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    await terminating
    assert not plugin.io_tasks


async def test_native_images_are_not_viewed_again(
    plugin, image_module, png_bytes, tmp_path
):
    path = tmp_path / "image.png"
    path.write_bytes(png_bytes)
    event = Event([NS(type="Image", file=str(path))])
    request = NS(contexts=[], extra_user_content_parts=[], image_urls=[str(path)])
    await plugin.add_index(event, request)
    assert "无需再用 image_cache_view" in request.extra_user_content_parts[0].text
    later = NS(contexts=[], extra_user_content_parts=[], image_urls=[])
    await plugin.add_index(Event(message_id="m2"), later)
    assert "无需再用" not in later.extra_user_content_parts[0].text


async def test_view_refuses_text_only_model(plugin, png_bytes):
    from astrbot_plugin_quote_cache.image_extraction import Candidate

    event = Event()
    image_id = plugin.cache.store(
        plugin.scope_of(event), Candidate("unused", "m1", "当前消息"), png_bytes
    )
    plugin.context = Context(["text"])
    result = await plugin.view_images(event, [image_id])
    assert result.isError and "未启用图片输入" in result.content[0].text
    plugin.context = Context(["text", "image"])
    assert not (await plugin.view_images(event, image_id)).isError
    assert not (await plugin.view_images(event, json.dumps([image_id]))).isError


async def test_isolated_group_sessions_share_images(plugin, png_bytes):
    from astrbot_plugin_quote_cache.image_extraction import Candidate

    alice = Event(scope="qq_official:GroupMessage:u1_g1", group_id="g1")
    bob = Event(scope="qq_official:GroupMessage:u2_g1", group_id="g1")
    image_id = plugin.cache.store(
        plugin.scope_of(alice), Candidate("unused", "m1", "当前消息"), png_bytes
    )
    assert not (await plugin.view_images(bob, [image_id])).isError
    assert (await plugin.view_images(Event(group_id="g2"), [image_id])).isError


async def test_request_waits_for_earlier_downloads(
    plugin, image_module, png_bytes, monkeypatch
):
    release = asyncio.Event()

    async def download(*args, **kwargs):
        await release.wait()
        return png_bytes

    monkeypatch.setattr(image_module, "read_source", download)
    sent = Event(raw={"content": "https://gchat.qpic.cn/a"}, message_id="m1")
    capture = asyncio.create_task(plugin.capture(sent))
    while not plugin.download_tasks:
        await asyncio.sleep(0)
    request = NS(contexts=[], extra_user_content_parts=[], image_urls=[])
    asking = asyncio.create_task(plugin.add_index(Event(message_id="m2"), request))
    await asyncio.sleep(0)
    assert not asking.done()
    release.set()
    await asyncio.gather(capture, asking)
    assert sent.get_extra(image_module.EVENT_KEY)["ids"][0] in (
        request.extra_user_content_parts[0].text
    )


async def test_quoted_bot_images_are_attributed_to_the_bot(
    plugin, image_module, tmp_path, png_bytes
):
    path = tmp_path / "drawn.png"
    path.write_bytes(png_bytes)
    reply = NS(
        type="Reply",
        id="bot-msg",
        sender_id="bot",
        sender_nickname="云云",
        chain=[NS(type="Image", file=str(path))],
    )
    event = Event(
        [reply, NS(type="Plain", text="这张图什么意思")],
        message_id="m2",
        quotes_bot=True,
    )
    request = NS(contexts=[], extra_user_content_parts=[], image_urls=[str(path)])
    await plugin.add_index(event, request)
    index = json.loads(
        request.extra_user_content_parts[0].text.removeprefix(image_module.INDEX_PREFIX)
    )
    assert "自己之前发的消息" in index["引用说明"]
    (row,) = index["最近图片"]
    assert row["sender_is_bot"] is True and row["origin"] == "引用"
    listed = json.loads(await plugin.list_images(event))["images"]
    assert listed[0]["sender_is_bot"] is True
    viewed = await plugin.view_images(event, [row["id"]])
    assert json.loads(viewed.content[0].text)["sender_is_bot"] is True
    # A user's own image keeps no bot flag, and quoting a user adds no note.
    user_reply = NS(type="Reply", id="u-msg", sender_id="u2", chain=[])
    other = NS(contexts=[], extra_user_content_parts=[], image_urls=[])
    await plugin.add_index(Event([user_reply], message_id="m3"), other)
    assert "引用说明" not in other.extra_user_content_parts[0].text


async def test_qq_official_quoted_bot_images_keep_stored_attribution(
    plugin, image_module, png_bytes, monkeypatch
):
    async def download(*args, **kwargs):
        return png_bytes

    monkeypatch.setattr(image_module, "read_source", download)

    def quote(ref):
        return {
            "message_type": 103,
            "message_scene": {"ext": ["msg_idx=REFIDX_new", f"ref_msg_idx={ref}"]},
            "msg_elements": [
                {
                    "attachments": [
                        {"content_type": "image/png", "url": "gchat.qpic.cn/b"}
                    ]
                }
            ],
        }

    event = Event(
        raw=quote("REFIDX_bot=="), self_id="qq_official", group_id="g1", quotes_bot=True
    )
    request = NS(contexts=[], extra_user_content_parts=[], image_urls=[])
    await plugin.add_index(event, request)
    index = json.loads(
        request.extra_user_content_parts[0].text.removeprefix(image_module.INDEX_PREFIX)
    )
    assert "自己之前发的消息" in index["引用说明"]
    assert index["最近图片"][0]["sender_is_bot"] is True
    # The flag is stored, so it survives a different self_id on later events.
    later = Event(self_id="1234", group_id="g1", message_id="m9")
    assert json.loads(await plugin.list_images(later))["images"][0]["sender_is_bot"]

    other = Event(raw=quote("REFIDX_user=="), group_id="g2", message_id="m2")
    request = NS(contexts=[], extra_user_content_parts=[], image_urls=[])
    await plugin.add_index(other, request)
    index = json.loads(
        request.extra_user_content_parts[0].text.removeprefix(image_module.INDEX_PREFIX)
    )
    assert "引用说明" not in index
    assert "sender_is_bot" not in index["最近图片"][0]
