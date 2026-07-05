from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable


CURRENT_ID_KEYS = {"id", "message_id", "msg_id", "msg_idx", "ref_idx"}
REFERENCE_ID_KEYS = {
    "ref_msg_idx",
    "reply_id",
    "reply_to",
    "reply_to_id",
    "quoted_message_id",
    "source_message_id",
    "reference_id",
}
MEDIA_TYPES = {"image", "record", "voice", "audio", "video", "file"}


def value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def clean_id(item: Any) -> str:
    if item is None or isinstance(item, (dict, list, tuple, set)):
        return ""
    text = str(item).strip()
    return text if text and text.lower() not in {"none", "null", "0"} else ""


def unique_aliases(items: Iterable[tuple[Any, str]]) -> list[tuple[str, str]]:
    result: dict[str, str] = {}
    for raw, kind in items:
        alias = clean_id(raw)
        if alias:
            result.setdefault(alias, kind)
    return list(result.items())


def parse_scene_ext(ext: Any) -> tuple[str, str]:
    ref_idx = ""
    msg_idx = ""
    if isinstance(ext, dict):
        ref_idx = clean_id(ext.get("ref_msg_idx") or ext.get("ref_idx"))
        msg_idx = clean_id(ext.get("msg_idx"))
        ext = list(ext.values())
    if isinstance(ext, str):
        ext = [ext]
    if isinstance(ext, (list, tuple)):
        for item in ext:
            if isinstance(item, dict):
                nested_ref, nested_msg = parse_scene_ext(item)
                ref_idx = nested_ref or ref_idx
                msg_idx = nested_msg or msg_idx
                continue
            text = str(item or "")
            ref_match = re.search(r"(?:^|[;,&\s])ref_msg_idx=([^;,&\s]+)", text)
            msg_match = re.search(r"(?:^|[;,&\s])msg_idx=([^;,&\s]+)", text)
            if ref_match:
                ref_idx = clean_id(ref_match.group(1))
            if msg_match:
                msg_idx = clean_id(msg_match.group(1))
    return ref_idx, msg_idx


def scene_indices(source: Any) -> tuple[str, str]:
    scene = value(source, "message_scene")
    return parse_scene_ext(value(scene, "ext", scene))


def message_type_number(source: Any) -> int | None:
    raw = value(source, "message_type", value(source, "msg_type"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _reply_components(event: Any) -> list[Any]:
    message_obj = value(event, "message_obj")
    chain = value(message_obj, "message", []) or []
    return [comp for comp in chain if comp.__class__.__name__.lower() in {"reply", "quote"}]


def current_aliases(event: Any) -> list[tuple[str, str]]:
    message_obj = value(event, "message_obj")
    raw = value(message_obj, "raw_message")
    extras = event.get_extra() if callable(getattr(event, "get_extra", None)) else {}
    pairs: list[tuple[Any, str]] = [
        (value(message_obj, "message_id"), "astr_message_id"),
        (value(raw, "id"), "raw_id"),
        (value(raw, "message_id"), "raw_message_id"),
        (value(raw, "msg_id"), "raw_msg_id"),
    ]
    for owner, prefix in ((raw, "raw"), (extras, "event_extra")):
        _, msg_idx = scene_indices(owner)
        pairs.append((msg_idx, f"{prefix}_msg_idx"))
        for key in ("msg_idx", "ref_idx"):
            pairs.append((value(owner, key), f"{prefix}_{key}"))
    return unique_aliases(pairs)


def reference_aliases(event: Any) -> list[str]:
    message_obj = value(event, "message_obj")
    raw = value(message_obj, "raw_message")
    extras = event.get_extra() if callable(getattr(event, "get_extra", None)) else {}
    pairs: list[tuple[Any, str]] = []
    for comp in _reply_components(event):
        pairs.append((value(comp, "id"), "reply_component"))
    for owner, prefix in ((raw, "raw"), (extras, "event_extra")):
        ref_idx, _ = scene_indices(owner)
        pairs.append((ref_idx, f"{prefix}_ref_msg_idx"))
        for key in REFERENCE_ID_KEYS:
            candidate = value(owner, key)
            if isinstance(candidate, dict):
                candidate = (
                    candidate.get("message_id")
                    or candidate.get("id")
                    or candidate.get("msg_idx")
                )
            pairs.append((candidate, f"{prefix}_{key}"))
    elements = value(raw, "msg_elements") or value(extras, "msg_elements") or []
    if message_type_number(raw) == 103 and elements:
        pairs.append((value(elements[0], "msg_idx"), "quote_element_msg_idx"))
    return [alias for alias, _ in unique_aliases(pairs)]


def _scalar_snapshot(obj: Any, keys: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in keys:
        item = value(obj, key)
        if item is None:
            continue
        if isinstance(item, (str, int, float, bool)):
            text = str(item)
            result[key] = text[:2000] if isinstance(item, str) else item
    return result


def component_record(comp: Any, depth: int = 0) -> dict[str, Any]:
    kind = comp.__class__.__name__.lower()
    data = _scalar_snapshot(
        comp,
        (
            "text", "id", "qq", "name", "file", "file_", "url", "path",
            "content_type", "mime_type", "size", "width", "height", "duration",
            "title", "content", "filename", "sender_id", "sender_nickname", "time",
        ),
    )
    data["type"] = kind
    if depth < 2:
        nested = value(comp, "chain") or value(comp, "nodes")
        if isinstance(nested, (list, tuple)):
            data["children"] = [component_record(x, depth + 1) for x in nested[:30]]
    return data


def attachment_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(record.get("type", "")).lower()
    if kind not in MEDIA_TYPES:
        return None
    normalized = {
        "type": "audio" if kind in {"record", "voice"} else kind,
        "filename": record.get("name") or record.get("filename") or "",
        "content_type": record.get("content_type") or record.get("mime_type") or "",
        "url": record.get("url") or "",
        "file": record.get("file") or record.get("file_") or "",
        "path": record.get("path") or "",
        "size": record.get("size") or 0,
        "width": record.get("width") or 0,
        "height": record.get("height") or 0,
        "duration": record.get("duration") or 0,
    }
    normalized["source"] = normalized["path"] or normalized["url"] or normalized["file"]
    return normalized


def serialize_chain(chain: Iterable[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    components: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    outline: list[str] = []
    for comp in list(chain or []):
        record = component_record(comp)
        components.append(record)
        kind = record["type"]
        if kind == "plain" and record.get("text"):
            outline.append(str(record["text"]))
        elif kind in MEDIA_TYPES:
            att = attachment_from_record(record)
            if att:
                attachments.append(att)
                outline.append(f"[{att['type']}:{att.get('filename') or 'attachment'}]")
        elif kind == "face":
            outline.append(f"[表情:{record.get('id', '')}]")
        elif kind in {"reply", "quote"}:
            continue
        elif kind == "at":
            outline.append(f"[@{record.get('name') or record.get('qq') or ''}]")
        else:
            outline.append(f"[{kind}]")
    return components, attachments, " ".join(x for x in outline if x).strip()


def raw_attachments(source: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in value(source, "attachments", []) or []:
        record = _scalar_snapshot(
            item,
            ("url", "filename", "content_type", "size", "height", "width", "duration", "voice_wav_url", "asr_refer_text"),
        )
        content_type = str(record.get("content_type", "")).lower()
        if record.get("voice_wav_url") or record.get("asr_refer_text"):
            kind = "audio"
        elif content_type.startswith("image/"):
            kind = "image"
        elif content_type.startswith("audio/"):
            kind = "audio"
        elif content_type.startswith("video/"):
            kind = "video"
        else:
            kind = "file"
        record.update(
            {
                "type": kind,
                "source": record.get("voice_wav_url") or record.get("url") or "",
                "file": "",
                "path": "",
            }
        )
        result.append(record)
    return result


def embedded_quote(event: Any) -> dict[str, Any] | None:
    message_obj = value(event, "message_obj")
    raw = value(message_obj, "raw_message")
    extras = event.get_extra() if callable(getattr(event, "get_extra", None)) else {}
    msg_type = message_type_number(raw)
    if msg_type is None:
        msg_type = message_type_number(extras)
    elements = value(raw, "msg_elements") or value(extras, "msg_elements") or []
    if msg_type == 103 and elements:
        node = elements[0]
        return {
            "content": str(value(node, "content", "") or ""),
            "attachments": raw_attachments(node),
            "ref_index": clean_id(value(node, "msg_idx")),
            "source": "msg_elements[0]",
        }
    for comp in _reply_components(event):
        chain = value(comp, "chain") or []
        components, attachments, outline = serialize_chain(chain)
        content = str(value(comp, "message_str", "") or outline)
        if content or attachments:
            return {
                "content": content,
                "attachments": attachments,
                "components": components,
                "ref_index": clean_id(value(comp, "id")),
                "sender_id": clean_id(value(comp, "sender_id")),
                "sender_name": str(value(comp, "sender_nickname", "") or ""),
                "timestamp": int(value(comp, "time", 0) or 0),
                "source": "Reply.chain",
            }
    return None


def raw_metadata(event: Any) -> dict[str, Any]:
    message_obj = value(event, "message_obj")
    raw = value(message_obj, "raw_message")
    extras = event.get_extra() if callable(getattr(event, "get_extra", None)) else {}
    ref_idx, msg_idx = scene_indices(raw)
    if not (ref_idx or msg_idx):
        ref_idx, msg_idx = scene_indices(extras)
    raw_keys = sorted(str(k) for k in getattr(raw, "__dict__", {}).keys())[:200]
    if isinstance(raw, dict):
        raw_keys = sorted(str(k) for k in raw.keys())[:200]
    return {
        "raw_type": type(raw).__name__,
        "raw_keys": raw_keys,
        "message_type": message_type_number(raw),
        "ref_msg_idx": ref_idx,
        "msg_idx": msg_idx,
        "event_extra_keys": sorted(str(k) for k in extras.keys()) if isinstance(extras, dict) else [],
    }


def outgoing_ids(response: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[Any, str]] = []
    owners = [response]
    if response is not None and not isinstance(response, dict):
        owners.append(getattr(response, "__dict__", {}))
    for owner in owners:
        for key in ("ref_idx", "ref_id", "msg_idx", "id", "message_id", "msg_id"):
            pairs.append((value(owner, key), f"send_response_{key}"))
    return unique_aliases(pairs)


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
