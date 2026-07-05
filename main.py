from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - compatibility with older AstrBot
    TextPart = None

from .cache_store import CachedMessage, MessageStore
from .media_cache import MediaCache
from .message_codec import (
    current_aliases,
    embedded_quote,
    outgoing_ids,
    parse_timestamp,
    raw_attachments,
    raw_metadata,
    reference_aliases,
    serialize_chain,
    value,
)
from .raw_payload_bridge import RawPayloadBridge


PLUGIN_NAME = "astrbot_plugin_quote_cache"
DEFAULT_PLATFORMS = {"qq_official", "qq_official_webhook"}
TEXT_PREVIEW_SUFFIXES = {
    ".txt", ".md", ".json", ".jsonl", ".csv", ".tsv", ".log", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".py", ".js", ".ts",
    ".html", ".css", ".java", ".go", ".rs", ".sh", ".ps1",
}


def _config_bool(config: dict, key: str, default: bool) -> bool:
    raw = config.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "是"}


def _config_int(config: dict, key: str, default: int, minimum: int = 0) -> int:
    try:
        return max(int(config.get(key, default)), minimum)
    except (TypeError, ValueError):
        return default


def _split_list(raw: Any) -> set[str]:
    if isinstance(raw, (list, tuple, set)):
        return {str(x).strip().lower() for x in raw if str(x).strip()}
    return {
        x.strip().lower()
        for x in str(raw or "").replace(",", "\n").splitlines()
        if x.strip()
    }


class QuoteCachePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.enabled = _config_bool(self.config, "enabled", True)
        self.platform_allowlist = _split_list(
            self.config.get("platform_allowlist", "qq_official,qq_official_webhook")
        )
        ttl_hours = _config_int(self.config, "ttl_hours", 48, 1)
        max_entries = _config_int(self.config, "max_entries", 50000, 100)
        self.data_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        ).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._store_path = self.data_dir / "messages.sqlite3"
        self._store_ttl_seconds = ttl_hours * 3600
        self._store_max_entries = max_entries
        self.store = self._new_store()
        self.media = MediaCache(
            self.data_dir / "media",
            enabled=_config_bool(self.config, "persist_attachments", True),
            max_bytes=_config_int(self.config, "max_attachment_mb", 50, 1)
            * 1024
            * 1024,
            timeout=_config_int(self.config, "download_timeout_seconds", 20, 3),
        )
        self.capture_outgoing = _config_bool(
            self.config, "capture_outgoing_ids", True
        )
        self.capture_raw_payload = _config_bool(
            self.config, "capture_raw_payload_bridge", True
        )
        self.inject_images = _config_bool(self.config, "inject_images", True)
        self.inject_audio = _config_bool(self.config, "inject_audio", True)
        self.max_injected_text = _config_int(
            self.config, "max_injected_text_length", 12000, 500
        )
        self.file_preview_chars = _config_int(
            self.config, "file_preview_chars", 8000, 0
        )
        self.admin_ids = _split_list(self.config.get("admin_user_ids", ""))
        self.debug_log = _config_bool(self.config, "debug_log", False)
        self.auto_cleanup_enabled = _config_bool(
            self.config, "auto_cleanup_enabled", True
        )
        self.cleanup_minutes = _config_int(
            self.config, "cleanup_interval_minutes", 60, 5
        )
        self._cleanup_task: asyncio.Task | None = None
        self._store_lock = asyncio.Lock()
        self.raw_bridge = RawPayloadBridge()

    def _new_store(self) -> MessageStore:
        return MessageStore(
            self._store_path,
            ttl_seconds=self._store_ttl_seconds,
            max_entries=self._store_max_entries,
        )

    async def initialize(self) -> None:
        if self.store.closed:
            self.store = self._new_store()
        bridge_installed = self.raw_bridge.install() if self.capture_raw_payload else False
        removed, paths = self.store.cleanup_expired()
        media_removed = self.media.remove_paths(paths)
        logger.info(
            "[quote-cache] ready: db=%s, ttl=%sh, expired=%s, media_removed=%s, raw_bridge=%s",
            self.store.db_path,
            self.store.ttl_seconds // 3600,
            removed,
            media_removed,
            bridge_installed,
        )
        if self.auto_cleanup_enabled and (
            self._cleanup_task is None or self._cleanup_task.done()
        ):
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(), name="astrbot-quote-cache-cleanup"
            )

    async def terminate(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            finally:
                self._cleanup_task = None
        try:
            await self.media.close()
        finally:
            self.raw_bridge.restore()
            self.store.close()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cleanup_minutes * 60)
            try:
                removed, paths = self.store.cleanup_expired()
                media_removed = self.media.remove_paths(paths)
                if removed or self.debug_log:
                    logger.info(
                        "[quote-cache] scheduled cleanup: messages=%s, media=%s",
                        removed,
                        media_removed,
                    )
            except Exception:
                logger.exception("[quote-cache] scheduled cleanup failed")

    def _platform_allowed(self, event: AstrMessageEvent) -> bool:
        if not self.enabled:
            return False
        if not self.platform_allowlist:
            return True
        names = {
            str(event.get_platform_name() or "").lower(),
            str(event.get_platform_id() or "").lower(),
        }
        return bool(names & self.platform_allowlist)

    @staticmethod
    def _scope_key(event: AstrMessageEvent) -> str:
        platform_id = str(event.get_platform_id() or event.get_platform_name() or "unknown")
        group_id = str(event.get_group_id() or "")
        if group_id:
            return f"{platform_id}|group:{group_id}"
        return f"{platform_id}|private:{event.get_session_id()}"

    @staticmethod
    def _message_type(event: AstrMessageEvent) -> str:
        try:
            raw = event.get_message_type()
            return str(getattr(raw, "value", raw) or "")
        except Exception:
            return ""

    @staticmethod
    def _bot_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_self_id() or "")
        except Exception:
            return str(value(event.message_obj, "self_id", "") or "")

    async def _persist_attachments(self, attachments: list[dict]) -> list[dict]:
        return await self.media.persist_all(attachments)

    async def _cache_inbound(self, event: AstrMessageEvent) -> None:
        message_obj = event.message_obj
        raw = value(message_obj, "raw_message")
        captured = event.get_extra("quote_cache_raw_payload", None)
        source = captured or raw
        components, attachments, outline = serialize_chain(
            value(message_obj, "message", []) or []
        )
        if not attachments:
            attachments = raw_attachments(source)
        attachments = await self._persist_attachments(attachments)
        aliases = current_aliases(event)
        alias_map = dict(aliases)
        astr_id = str(value(message_obj, "message_id", "") or "")
        original_id = str(
            value(source, "id", "")
            or value(source, "message_id", "")
            or value(source, "msg_id", "")
            or ""
        )
        ref_index = next(
            (
                alias
                for alias, kind in aliases
                if "msg_idx" in kind or "ref_idx" in kind
            ),
            "",
        )
        metadata = raw_metadata(event)
        author = value(source, "author")
        sender_is_bot = bool(value(author, "bot", False)) or (
            bool(self._bot_id(event))
            and str(event.get_sender_id() or "") == self._bot_id(event)
        )
        now = int(time.time())
        timestamp = parse_timestamp(
            value(message_obj, "timestamp", value(source, "timestamp")), now
        )
        record = CachedMessage(
            scope_key=self._scope_key(event),
            platform_id=str(event.get_platform_id() or ""),
            platform_name=str(event.get_platform_name() or ""),
            session_id=str(event.get_session_id() or ""),
            group_id=str(event.get_group_id() or ""),
            message_type=self._message_type(event),
            astr_message_id=astr_id,
            original_message_id=original_id,
            ref_index=ref_index,
            content=str(event.get_message_str() or outline or ""),
            sender_id=str(event.get_sender_id() or ""),
            sender_name=str(event.get_sender_name() or ""),
            timestamp=timestamp,
            is_bot=sender_is_bot,
            attachments=attachments,
            components=components,
            raw_meta=metadata,
        )
        async with self._store_lock:
            self.store.put(record, aliases)
        if self.debug_log:
            logger.info(
                "[quote-cache] inbound cached: scope=%s astr=%s original=%s aliases=%s attachments=%s",
                record.scope_key,
                astr_id,
                original_id,
                alias_map,
                len(attachments),
            )

    async def _cache_outgoing_response(
        self,
        event: AstrMessageEvent,
        response: Any,
        components: list[dict],
        attachments: list[dict],
        outline: str,
    ) -> None:
        ids = outgoing_ids(response)
        if not ids:
            if self.debug_log and (outline or attachments):
                logger.info("[quote-cache] outgoing response had no usable ID")
            return
        now = int(time.time())
        bot_id = self._bot_id(event) or "qq_official_bot"
        record = CachedMessage(
            scope_key=self._scope_key(event),
            platform_id=str(event.get_platform_id() or ""),
            platform_name=str(event.get_platform_name() or ""),
            session_id=str(event.get_session_id() or ""),
            group_id=str(event.get_group_id() or ""),
            message_type=self._message_type(event),
            astr_message_id=ids[0][0],
            original_message_id=next(
                (alias for alias, kind in ids if kind.endswith("_id")), ids[0][0]
            ),
            ref_index=next(
                (alias for alias, kind in ids if "ref_idx" in kind or "msg_idx" in kind),
                "",
            ),
            content=outline,
            sender_id=bot_id,
            sender_name="AstrBot",
            timestamp=now,
            is_bot=True,
            attachments=attachments,
            components=components,
            raw_meta={
                "source": "qq_official_send_response",
                "response_type": type(response).__name__,
            },
        )
        async with self._store_lock:
            self.store.put(record, ids)
        logger.info(
            "[quote-cache] outbound cached: aliases=%s attachments=%s",
            dict(ids),
            len(attachments),
        )

    def _install_send_capture(self, event: AstrMessageEvent) -> None:
        if not self.capture_outgoing or getattr(event, "_quote_cache_wrapped", False):
            return

        # Newer QQ Official adapters split rich chains into individual sends here.
        # Wrapping the per-message method keeps each returned ID tied to the exact
        # text/media chunk the user can later quote.
        original_one = getattr(event, "_post_send_one", None)
        if callable(original_one):
            event._quote_cache_wrapped = True

            async def wrapped_post_send_one(message_to_send, *args, **kwargs):
                chain = list(value(message_to_send, "chain", []) or [])
                components, attachments, outline = serialize_chain(chain)
                attachments = await self._persist_attachments(attachments)
                response = await original_one(message_to_send, *args, **kwargs)
                await self._cache_outgoing_response(
                    event, response, components, attachments, outline
                )
                return response

            event._post_send_one = wrapped_post_send_one
            return

        # Older adapters only expose the aggregate send method.
        original = getattr(event, "_post_send", None)
        if not callable(original):
            return
        event._quote_cache_wrapped = True

        async def wrapped_post_send(*args, **kwargs):
            send_buffer = getattr(event, "send_buffer", None)
            chain = list(value(send_buffer, "chain", []) or [])
            components, attachments, outline = serialize_chain(chain)
            attachments = await self._persist_attachments(attachments)
            response = await original(*args, **kwargs)
            await self._cache_outgoing_response(
                event, response, components, attachments, outline
            )
            return response

        event._post_send = wrapped_post_send

    def _ensure_reply_component(self, event: AstrMessageEvent) -> list[str]:
        """Materialize a Reply segment so an otherwise empty quote reaches the LLM.

        AstrBot's internal agent skips events with no text, media, provider request,
        or Reply component. QQ Official botpy does not create that component for
        quote payloads, so ``quote + @bot`` used to stop before on_llm_request.
        """
        chain = value(event.message_obj, "message", [])
        if not isinstance(chain, list):
            return reference_aliases(event)
        refs = reference_aliases(event)
        if not refs or any(isinstance(comp, Reply) for comp in chain):
            return refs
        entry = self.store.find(self._scope_key(event), refs)
        kwargs: dict[str, Any] = {"id": refs[0]}
        if entry:
            kwargs.update(
                {
                    "sender_id": entry.sender_id,
                    "sender_nickname": entry.sender_name,
                    "time": entry.timestamp,
                    "message_str": entry.content,
                }
            )
        chain.insert(0, Reply(**kwargs))
        event.set_extra(
            "quote_cache_synthetic_reply",
            {"reference_ids": refs, "cache_hit": bool(entry)},
        )
        logger.info(
            "[quote-cache] materialized Reply component: refs=%s cache_hit=%s",
            refs,
            bool(entry),
        )
        return refs

    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize)
    async def capture_message(self, event: AstrMessageEvent) -> None:
        if not self._platform_allowed(event):
            return
        payload = self.raw_bridge.take(value(event.message_obj, "raw_message"))
        if payload:
            event.set_extra("quote_cache_raw_payload", payload)
        self._ensure_reply_component(event)
        self._install_send_capture(event)
        try:
            await self._cache_inbound(event)
        except Exception:
            logger.exception("[quote-cache] failed to cache inbound message")

    async def _entry_from_embedded(
        self, event: AstrMessageEvent, data: dict
    ) -> CachedMessage:
        attachments = await self._persist_attachments(list(data.get("attachments") or []))
        now = int(time.time())
        return CachedMessage(
            scope_key=self._scope_key(event),
            platform_id=str(event.get_platform_id() or ""),
            platform_name=str(event.get_platform_name() or ""),
            session_id=str(event.get_session_id() or ""),
            group_id=str(event.get_group_id() or ""),
            message_type="quoted",
            astr_message_id=str(data.get("ref_index") or ""),
            original_message_id="",
            ref_index=str(data.get("ref_index") or ""),
            content=str(data.get("content") or ""),
            sender_id=str(data.get("sender_id") or ""),
            sender_name=str(data.get("sender_name") or ""),
            timestamp=int(data.get("timestamp") or now),
            attachments=attachments,
            components=list(data.get("components") or []),
            raw_meta={"source": data.get("source", "embedded_quote")},
        )

    def _file_preview(self, attachment: dict) -> str:
        if self.file_preview_chars <= 0 or attachment.get("type") != "file":
            return ""
        raw_path = attachment.get("local_path")
        if not raw_path:
            return ""
        path = Path(str(raw_path))
        if path.suffix.lower() not in TEXT_PREVIEW_SUFFIXES:
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")[: self.file_preview_chars]
        except OSError:
            return ""

    def _format_entry(self, entry: CachedMessage, matched_ids: list[str]) -> str:
        sender = entry.sender_name or entry.sender_id or "未知发送者"
        lines = [
            '<quoted_message source="astrbot_quote_cache">',
            f"发送者: {sender} (id={entry.sender_id or 'unknown'}, bot={str(entry.is_bot).lower()})",
            f"消息时间戳: {entry.timestamp}",
            f"AstrBot消息ID: {entry.astr_message_id or 'unknown'}",
        ]
        if entry.original_message_id:
            lines.append(f"平台原始消息ID: {entry.original_message_id}")
        if entry.ref_index:
            lines.append(f"QQ引用索引: {entry.ref_index}")
        if matched_ids:
            lines.append(f"本次引用ID: {', '.join(matched_ids[:5])}")
        lines.append("内容:")
        lines.append(entry.content or "[无文本内容]")
        if entry.attachments:
            lines.append("附件:")
            for index, att in enumerate(entry.attachments, 1):
                kind = att.get("type") or "unknown"
                name = att.get("filename") or Path(str(att.get("local_path") or att.get("source") or "attachment")).name
                ref = att.get("local_path") or att.get("url") or att.get("source") or "unavailable"
                details = [f"type={kind}", f"name={name}", f"ref={ref}"]
                if att.get("content_type"):
                    details.append(f"mime={att['content_type']}")
                if att.get("size") or att.get("cached_size"):
                    details.append(f"size={att.get('size') or att.get('cached_size')}")
                if att.get("asr_refer_text"):
                    details.append(f"transcript={att['asr_refer_text']}")
                lines.append(f"- {index}. " + ", ".join(details))
                preview = self._file_preview(att)
                if preview:
                    lines.extend([f"  文件文本预览开始({len(preview)}字符)", preview, "  文件文本预览结束"])
        lines.append("</quoted_message>")
        text = "\n".join(lines)
        if len(text) > self.max_injected_text:
            text = text[: self.max_injected_text] + "\n[引用内容因长度限制被截断]\n</quoted_message>"
        return text

    @staticmethod
    def _already_has_quote(req: ProviderRequest) -> bool:
        for part in getattr(req, "extra_user_content_parts", []) or []:
            text = str(getattr(part, "text", "") or "")
            has_marker = "<Quoted Message>" in text or "<quoted_message" in text
            is_empty = "[Empty Text]" in text or "原始内容不可用" in text
            if has_marker and not is_empty:
                return True
        return False

    def _inject_text(self, req: ProviderRequest, text: str) -> None:
        if not self._already_has_quote(req):
            parts = getattr(req, "extra_user_content_parts", None)
            if isinstance(parts, list) and TextPart is not None:
                parts.append(TextPart(text=text))
                return
            req.prompt = f"{text}\n\n{req.prompt or ''}".strip()

    def _inject_media(self, req: ProviderRequest, entry: CachedMessage) -> None:
        image_urls = getattr(req, "image_urls", None)
        audio_urls = getattr(req, "audio_urls", None)
        for att in entry.attachments:
            ref = str(att.get("local_path") or att.get("url") or att.get("source") or "")
            if not ref:
                continue
            kind = str(att.get("type") or "").lower()
            if kind == "image" and self.inject_images and isinstance(image_urls, list):
                if ref not in image_urls:
                    image_urls.append(ref)
            elif kind in {"audio", "voice", "record"} and self.inject_audio and isinstance(audio_urls, list):
                if ref not in audio_urls:
                    audio_urls.append(ref)

    @filter.on_llm_request(priority=sys.maxsize - 20)
    async def inject_quote(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self._platform_allowed(event):
            return
        refs = reference_aliases(event)
        fallback = embedded_quote(event)
        if not refs and not fallback:
            return
        scope = self._scope_key(event)
        entry = self.store.find(scope, refs)
        source = "cache"
        if entry is None:
            if fallback:
                entry = await self._entry_from_embedded(event, fallback)
                source = str(fallback.get("source") or "embedded")
                if entry.ref_index:
                    self.store.put(entry, [(entry.ref_index, "embedded_quote_ref")])
        if entry is None:
            text = (
                '<quoted_message source="astrbot_quote_cache">\n'
                f"引用ID: {', '.join(refs[:5])}\n"
                "原始内容不可用：本地缓存未命中，事件中也没有内嵌引用内容。\n"
                "</quoted_message>"
            )
            self._inject_text(req, text)
            event.set_extra("quote_cache_result", {"hit": False, "reference_ids": refs})
            logger.info("[quote-cache] quote miss: scope=%s refs=%s", scope, refs)
            return
        self._inject_text(req, self._format_entry(entry, refs))
        self._inject_media(req, entry)
        event.set_extra(
            "quote_cache_result",
            {
                "hit": True,
                "source": source,
                "reference_ids": refs,
                "cached_message_id": entry.db_id,
                "astr_message_id": entry.astr_message_id,
                "original_message_id": entry.original_message_id,
                "ref_index": entry.ref_index,
                "attachments": len(entry.attachments),
            },
        )
        logger.info(
            "[quote-cache] quote hit: source=%s scope=%s refs=%s cached_id=%s attachments=%s",
            source,
            scope,
            refs,
            entry.db_id,
            len(entry.attachments),
        )

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "").lower()
        return bool(event.is_admin() or (sender and sender in self.admin_ids))

    @filter.command("引用缓存清理")
    async def clean_cache(self, event: AstrMessageEvent, mode: str = ""):
        """清理过期引用缓存；参数可用“当前会话”或“全部”。"""
        if not self._is_admin(event):
            yield event.plain_result("这个指令只允许 AstrBot 管理员使用。")
            return
        mode = str(mode or "").strip().lower()
        if mode in {"全部", "all"}:
            removed, paths = self.store.clear()
            label = "全部"
        elif mode in {"当前会话", "会话", "current", "session"}:
            removed, paths = self.store.clear(self._scope_key(event))
            label = "当前会话"
        else:
            removed, paths = self.store.cleanup_expired()
            label = "已过期"
        media_removed = self.media.remove_paths(paths)
        yield event.plain_result(
            f"已清理{label}引用缓存：{removed} 条消息，{media_removed} 个媒体文件。"
        )

    @filter.command("引用缓存状态")
    async def cache_status(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("这个指令只允许 AstrBot 管理员使用。")
            return
        all_stats = self.store.stats()
        scope_stats = self.store.stats(self._scope_key(event))
        yield event.plain_result(
            "引用缓存状态\n"
            f"当前会话：{scope_stats['messages']} 条\n"
            f"全库：{all_stats['messages']} 条 / {all_stats['aliases']} 个索引\n"
            f"TTL：{all_stats['ttl_seconds'] // 3600} 小时\n"
            f"数据库：{all_stats['db_path']}\n"
            f"媒体目录：{self.media.root}"
        )

    @filter.command("引用缓存调试")
    async def debug_quote(self, event: AstrMessageEvent):
        """回复一条消息后使用，显示本次事件可见的引用字段。"""
        if not self._is_admin(event):
            yield event.plain_result("这个指令只允许 AstrBot 管理员使用。")
            return
        metadata = raw_metadata(event)
        current = current_aliases(event)
        refs = reference_aliases(event)
        fallback = embedded_quote(event)
        hit = self.store.find(self._scope_key(event), refs)
        yield event.plain_result(
            "引用缓存调试\n"
            f"raw 类型：{metadata.get('raw_type')}\n"
            f"raw 字段：{', '.join(metadata.get('raw_keys', [])) or '(空)'}\n"
            f"捕获原始 payload：{'是' if metadata.get('captured_payload') else '否'}\n"
            f"payload 字段：{', '.join(metadata.get('captured_keys', [])) or '(空)'}\n"
            f"event extra：{', '.join(metadata.get('event_extra_keys', [])) or '(空)'}\n"
            f"message_type：{metadata.get('message_type')}\n"
            f"message_reference：{metadata.get('message_reference_id') or '(无)'}\n"
            f"当前消息索引：{current or '(未发现)'}\n"
            f"引用目标索引：{refs or '(未发现)'}\n"
            f"内嵌引用：{fallback.get('source') if fallback else '(无)'}\n"
            f"缓存命中：{'是，db_id=' + str(hit.db_id) if hit else '否'}"
        )
