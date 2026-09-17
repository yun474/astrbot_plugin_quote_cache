from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable


def value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def clean_id(item: Any) -> str:
    if item is None or isinstance(item, (dict, list, tuple, set)):
        return ""
    text = str(item).strip()
    return text if text and text.lower() not in {"none", "null", "0"} else ""


def parse_timestamp(raw: Any, fallback: int) -> int:
    if isinstance(raw, (int, float)):
        value_int = int(raw)
        return value_int // 1000 if value_int > 20_000_000_000 else value_int
    if isinstance(raw, str) and raw.strip():
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            try:
                return int(float(raw))
            except ValueError:
                pass
    return fallback


def message_aliases(message_obj: Any) -> list[tuple[str, str]]:
    """Collect stable IDs without depending on a specific platform adapter."""
    raw = value(message_obj, "raw_message")
    pairs = [
        (value(message_obj, "message_id"), "astr_message_id"),
        (value(raw, "id"), "raw_id"),
        (value(raw, "message_id"), "raw_message_id"),
        (value(raw, "msg_id"), "raw_msg_id"),
    ]
    result: dict[str, str] = {}
    for raw_id, kind in pairs:
        item = clean_id(raw_id)
        if item:
            result.setdefault(item, kind)
    return list(result.items())


def message_identifiers(message_obj: Any) -> tuple[str, str, str]:
    """Return the native ID, own quote index, and quoted target index.

    AstrBot preserves QQ's unmodelled fields in raw_message.raw_data.
    Never use Reply.id or nested msg_elements as this message's identity.
    """
    raw = value(message_obj, "raw_message")
    data = value(raw, "raw_data", raw)
    native_id = clean_id(value(message_obj, "message_id"))
    for key in ("id", "message_id", "msg_id"):
        candidate = clean_id(value(data, key) or value(raw, key))
        if candidate:
            native_id = candidate
            break
    scene = value(data, "message_scene")
    indices = {}
    for entry in value(scene, "ext", []) or []:
        if isinstance(entry, str):
            key, _, item = entry.partition("=")
            if key in {"msg_idx", "ref_msg_idx"}:
                indices[key] = clean_id(item)
    return native_id, indices.get("msg_idx", ""), indices.get("ref_msg_idx", "")


def sent_message_outline(payload: dict) -> tuple[str, list[dict[str, str]]]:
    """Keep searchable sent text and media placeholders, never upload tokens."""
    text = value(payload.get("markdown"), "content") or payload.get("content")
    chain = [{"type": "plain", "text": text}] if text else []
    if payload.get("media"):
        chain.append({"type": "富媒体"})
    if not chain:
        chain.append({"type": "消息"})
    return history_outline(chain)


def extract_group_name(message_obj: Any) -> str:
    """Read a group name exposed by the adapter without making network calls."""
    raw = value(message_obj, "raw_message")
    candidates = [
        value(value(message_obj, "group"), "group_name"),
        value(raw, "group_name"),
        value(raw, "guild_name"),
        value(raw, "channel_name"),
        value(value(raw, "group"), "name"),
        value(value(raw, "guild"), "name"),
        value(value(raw, "channel"), "name"),
    ]
    for candidate in candidates:
        if candidate is None or isinstance(candidate, (dict, list, tuple, set)):
            continue
        text = str(candidate).strip()
        if text and text.casefold() not in {"n/a", "none", "null"}:
            return text
    return ""


def _component_type(component: Any) -> str:
    class_name = component.__class__.__name__.lower()
    if class_name not in {"dict", "object", "basemessagecomponent"}:
        return class_name
    raw = value(component, "type", "")
    raw = getattr(raw, "value", raw)
    return str(raw or class_name).split(".")[-1].lower()


def _component_text(component: Any, *keys: str) -> str:
    for key in keys:
        item = value(component, key)
        if item not in (None, ""):
            return str(item)
    return ""


def history_outline(
    chain: Iterable[Any] | None,
    fallback_text: str = "",
) -> tuple[str, list[dict[str, str]]]:
    """Build searchable text while deliberately discarding media URLs and bytes."""
    parts: list[str] = []
    components: list[dict[str, str]] = []
    has_plain_text = False

    for component in list(chain or []):
        kind = _component_type(component)
        record: dict[str, str] = {"type": kind}
        placeholder = ""

        if kind in {"plain", "text"}:
            text = _component_text(component, "text", "content")
            if text:
                parts.append(text)
                record["text"] = text
                has_plain_text = True
        elif kind in {"image", "picture", "cardimage"}:
            placeholder = "[图片]"
        elif kind in {"record", "voice", "audio", "tts"}:
            placeholder = "[语音]"
        elif kind == "video":
            placeholder = "[视频]"
        elif kind == "file":
            filename = _component_text(component, "name", "filename")
            placeholder = f"[文件:{filename}]" if filename else "[文件]"
        elif kind == "face":
            face_id = _component_text(component, "id")
            placeholder = f"[表情:{face_id}]" if face_id else "[表情]"
        elif kind == "at":
            target = _component_text(component, "name", "qq")
            placeholder = f"[@{target}]" if target else "[@用户]"
        elif kind in {"atall", "at_all"}:
            placeholder = "[@全体成员]"
        elif kind in {"reply", "quote"}:
            placeholder = "[引用消息]"
        elif kind in {"forward", "node", "nodes"}:
            placeholder = "[转发消息]"
        else:
            placeholder = f"[{kind or '消息段'}]"

        if placeholder:
            parts.append(placeholder)
            record["placeholder"] = placeholder
        components.append(record)

    fallback_text = str(fallback_text or "").strip()
    if fallback_text and not has_plain_text:
        parts.insert(0, fallback_text)

    return " ".join(part.strip() for part in parts if part.strip()).strip(), components
