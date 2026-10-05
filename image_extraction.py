"""Extract structured images and QQ CDN references without downloading media."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

QQ_IMAGE_HOSTS = (
    "qpic.cn",
    "qlogo.cn",
    "multimedia.nt.qq.com",
    "multimedia.nt.qq.com.cn",
)
URL_PATTERN = re.compile(r"https?://[^\s<>\"'\[\]]+", re.IGNORECASE)
CHILD_KEYS = (
    "msg_elements",
    "elements",
    "messages",
    "nodes",
    "message",
    "content",
    "data",
    "text",
)


def field(obj, key, default=None):
    return (
        obj.get(key, default)
        if isinstance(obj, Mapping)
        else getattr(obj, key, default)
    )


def kind(obj):
    value = field(obj, "type", "")
    return str(getattr(value, "value", value)).lower()


def qq_image_url(value: str) -> str | None:
    """Recognize QQ image hosts while preserving signed URL query strings."""
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    elif "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme in ("http", "https") and any(
            host == suffix or host.endswith("." + suffix) for suffix in QQ_IMAGE_HOSTS
        ):
            return value
    except ValueError:
        pass
    return None


@dataclass(frozen=True)
class Candidate:
    ref: str
    message_id: str
    origin: str
    sender_id: str = ""
    sender_name: str = ""
    timestamp: float = 0
    local_allowed: bool = False
    is_bot: bool = False


def collect_images(
    chain,
    raw,
    *,
    message_id,
    sender_id,
    sender_name,
    timestamp,
    limit=20,
):
    """Collect a bounded image list, retaining reply and forward provenance.

    Args:
        chain: AstrBot components. Only actual image components allow local paths.
        raw: QQ official raw payload, or None when only the chain is checked.
        message_id: Incoming message identifier.
        sender_id: Incoming sender identifier.
        sender_name: Incoming sender display name.
        timestamp: Incoming event time in Unix seconds.
        limit: Maximum number of candidates to return.

    Returns:
        Image candidates in message order, deduplicated by reference and origin.
    """
    found = []
    seen = set()
    visited = 0
    represented = {message_id: sum(kind(c) == "image" for c in chain)}
    for component in chain:
        if kind(component) == "reply" and field(component, "id"):
            represented[str(field(component, "id"))] = sum(
                kind(c) == "image" for c in (field(component, "chain") or [])
            )

    def add(ref, mid, origin, sid, name, when, local=False):
        if not isinstance(ref, str) or not ref.strip() or len(found) >= limit:
            return
        key = (mid, origin, ref)
        if key not in seen:
            seen.add(key)
            found.append(Candidate(ref, mid, origin, sid, name, when, local))

    def walk(obj, mid, origin, sid, name, when, depth=0, structured=False):
        nonlocal visited
        visited += 1
        if depth > 8 or visited > 1024 or len(found) >= limit:
            return
        if isinstance(obj, str):
            if raw is not None:
                for match in URL_PATTERN.finditer(obj):
                    if len(found) >= limit:
                        break
                    url = qq_image_url(match.group().rstrip(".,;!，。；！)）"))
                    if url:
                        add(url, mid, origin, sid, name, when)
            return
        if isinstance(obj, (list, tuple)):
            for index, item in enumerate(obj, 1):
                if visited >= 1024 or len(found) >= limit:
                    break
                node_origin = (
                    f"{origin}/{index}" if origin.startswith("转发") else origin
                )
                walk(item, mid, node_origin, sid, name, when, depth + 1, structured)
            return
        if obj is None:
            return
        t = kind(obj)
        if structured and t == "image":
            add(
                field(obj, "url") or field(obj, "file"),
                mid,
                origin,
                sid,
                name,
                when,
                True,
            )
            return
        if structured and t == "plain":
            walk(field(obj, "text", ""), mid, origin, sid, name, when, depth + 1)
            return
        if structured and t == "reply":
            mid = str(field(obj, "id") or mid)
            sid = str(field(obj, "sender_id") or "")
            name = str(field(obj, "sender_nickname") or "")
            walk(field(obj, "chain"), mid, "引用", sid, name, when, depth + 1, True)
            return
        if structured:
            return

        mid = str(field(obj, "id") or field(obj, "message_id") or mid)
        author = field(obj, "author") or field(obj, "sender")
        if author:
            sid = str(
                field(author, "member_openid")
                or field(author, "user_openid")
                or field(author, "id")
                or field(author, "user_id")
                or ""
            )
            name = str(field(author, "username") or field(author, "nickname") or "")
        # Attachments are structural evidence; extension-less signed URLs are valid.
        attachments = field(obj, "attachments", []) or []
        if not isinstance(attachments, (list, tuple)):
            attachments = []
        recognized = [
            attachment
            for attachment in attachments
            if str(
                field(attachment, "content_type")
                or field(attachment, "contentType")
                or ""
            )
            .lower()
            .startswith("image")
            or str(field(attachment, "filename") or field(attachment, "name") or "")
            .lower()
            .endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
        ]
        # Localized components no longer carry the original URL. Match the known
        # attachment count, but retain unknown CDN attachments the adapter missed.
        if represented.get(mid) and represented[mid] == len(recognized):
            attachments = [a for a in attachments if a not in recognized]
        for attachment in attachments:
            mime = str(
                field(attachment, "content_type")
                or field(attachment, "contentType")
                or ""
            ).lower()
            url = field(attachment, "url")
            filename = str(
                field(attachment, "filename") or field(attachment, "name") or ""
            ).lower()
            if isinstance(url, str) and (
                mime.startswith("image/")
                or filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
                or qq_image_url(url)
            ):
                normalized = (
                    url
                    if url.startswith(("http://", "https://"))
                    else "https:" + url
                    if url.startswith("//")
                    else "https://" + url
                )
                add(normalized, mid, origin, sid, name, when)
        if t == "image":
            data = field(obj, "data") or obj
            ref = field(data, "url") or field(data, "file")
            if isinstance(ref, str) and ref.startswith(("http://", "https://")):
                add(ref, mid, origin, sid, name, when)
        for key in CHILD_KEYS:
            child = field(obj, key)
            if child is not None:
                child_origin = origin
                if key in ("msg_elements", "messages", "nodes", "elements"):
                    child_origin = (
                        "引用" if str(field(obj, "message_type")) == "103" else "转发"
                    )
                child_sid, child_name = (
                    ("", "") if child_origin != origin else (sid, name)
                )
                walk(child, mid, child_origin, child_sid, child_name, when, depth + 1)

    walk(
        chain,
        message_id,
        "当前消息",
        sender_id,
        sender_name,
        timestamp,
        structured=True,
    )
    if raw is not None:
        walk(raw, message_id, "当前消息", sender_id, sender_name, timestamp)
    return found
