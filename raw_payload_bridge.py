from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from typing import Any


class RawPayloadBridge:
    """Capture QQ botpy payloads immediately before botpy discards extra fields.

    qq-botpy message classes use ``__slots__`` and only copy a fixed subset of
    the gateway payload. Patching the constructors is intentionally narrower
    than replacing an AstrBot platform adapter: normal parsing and dispatch are
    untouched, while a short-lived sidecar copy remains available to the Star
    event handler.
    """

    _MARKER = "_astrbot_quote_cache_bridge"

    def __init__(self, max_entries: int = 5000, ttl_seconds: int = 600):
        self.max_entries = max(int(max_entries), 100)
        self.ttl_seconds = max(int(ttl_seconds), 60)
        self._payloads: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.RLock()
        self._patched: list[tuple[type, Any, Any]] = []
        self._token = object()

    def install(self) -> bool:
        if self._patched:
            return True
        try:
            import botpy.message as botpy_message
        except ImportError:
            return False

        classes = [
            getattr(botpy_message, "BaseMessage", None),
            getattr(botpy_message, "Message", None),
            getattr(botpy_message, "DirectMessage", None),
        ]
        installed = False
        for cls in classes:
            if not isinstance(cls, type):
                continue
            current = cls.__init__
            previous_marker = getattr(current, self._MARKER, None)
            if isinstance(previous_marker, dict):
                # Recover from a plugin reload where the previous instance was
                # not cleanly terminated, then install the fresh bridge.
                current = previous_marker.get("original", current)

            bridge = self

            def wrapped(instance, *args, _original=current, **kwargs):
                data = kwargs.get("data")
                if data is None and len(args) >= 3:
                    data = args[2]
                bridge.capture(data)
                _original(instance, *args, **kwargs)

            setattr(
                wrapped,
                self._MARKER,
                {"original": current, "token": self._token},
            )
            cls.__init__ = wrapped
            self._patched.append((cls, current, wrapped))
            installed = True
        return installed

    def restore(self) -> None:
        for cls, original, wrapper in reversed(self._patched):
            if cls.__init__ is wrapper:
                cls.__init__ = original
        self._patched.clear()

    def capture(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        message_id = str(
            data.get("id") or data.get("message_id") or data.get("msg_id") or ""
        ).strip()
        if not message_id:
            return
        try:
            snapshot = copy.deepcopy(data)
        except Exception:
            snapshot = dict(data)
        now = time.monotonic()
        with self._lock:
            self._evict_locked(now)
            self._payloads[message_id] = (now, snapshot)
            self._payloads.move_to_end(message_id)
            while len(self._payloads) > self.max_entries:
                self._payloads.popitem(last=False)

    def take(self, message: Any) -> dict[str, Any] | None:
        message_id = ""
        if isinstance(message, dict):
            message_id = str(
                message.get("id")
                or message.get("message_id")
                or message.get("msg_id")
                or ""
            ).strip()
        else:
            message_id = str(
                getattr(message, "id", None)
                or getattr(message, "message_id", None)
                or getattr(message, "msg_id", None)
                or ""
            ).strip()
        if not message_id:
            return None
        with self._lock:
            self._evict_locked(time.monotonic())
            found = self._payloads.pop(message_id, None)
        return found[1] if found else None

    def _evict_locked(self, now: float) -> None:
        while self._payloads:
            _, (created, _) = next(iter(self._payloads.items()))
            if now - created <= self.ttl_seconds:
                break
            self._payloads.popitem(last=False)

    @property
    def pending(self) -> int:
        with self._lock:
            self._evict_locked(time.monotonic())
            return len(self._payloads)
