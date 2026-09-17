from __future__ import annotations

from functools import wraps


class QQSendObserver:
    """Observe successful QQ v2 sends at the common botpy HTTP boundary.

    C2C streaming bypasses BotAPI.post_c2c_message, and one event may send
    several messages. The request response is the shared source of true IDs.
    The callback selects only clients owned by AstrBot's QQ adapters.
    """

    def __init__(self, http_class, callback, logger):
        self.http_class = http_class
        self.callback = callback
        self.logger = logger
        self.original = http_class.request
        self.active = True

        @wraps(self.original)
        async def request(http, route, *args, **kwargs):
            response = await self.original(http, route, *args, **kwargs)
            if (
                self.active
                and route.method == "POST"
                and route.path
                in {
                    "/v2/groups/{group_openid}/messages",
                    "/v2/users/{openid}/messages",
                }
                and isinstance(response, dict)
                and response.get("id")
            ):
                try:
                    await self.callback(http, route, kwargs.get("json") or {}, response)
                except Exception:
                    # A cache failure must not turn a successful send into a retry.
                    self.logger.exception(
                        "[history-search] failed to cache QQ send receipt"
                    )
            return response

        self.wrapper = request
        http_class.request = request

    def close(self) -> None:
        self.active = False
        # Do not overwrite a wrapper installed afterwards by another plugin.
        if self.http_class.request is self.wrapper:
            self.http_class.request = self.original
