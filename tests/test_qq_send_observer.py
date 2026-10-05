import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_quote_cache.qq_send_observer import QQSendObserver


def route(path="/v2/groups/{group_openid}/messages", method="POST"):
    return SimpleNamespace(
        method=method, path=path, parameters={"group_openid": "group"}
    )


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class Http:
            async def request(self, route, *args, **kwargs):
                return await self.send(route, *args, **kwargs)

        self.http_class = Http
        self.http = Http()
        self.response = {"id": "sent", "ext_info": {"ref_idx": "REFIDX_own=="}}
        self.http.send = AsyncMock(return_value=self.response)
        self.callback = AsyncMock()
        self.logger = Mock()
        self.original = Http.request
        self.observer = QQSendObserver(Http, self.callback, self.logger)

    async def asyncTearDown(self):
        self.observer.close()

    async def test_success_preserves_request_and_exact_response(self):
        target = route()
        payload = {"msg_id": "incoming", "content": "hello"}
        result = await self.http.request(target, json=payload)
        self.assertIs(result, self.response)
        self.http.send.assert_awaited_once_with(target, json=payload)
        self.callback.assert_awaited_once_with(
            self.http, target, payload, self.response
        )

    async def test_failed_send_never_creates_receipt(self):
        self.http.send.side_effect = RuntimeError("send failed")
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await self.http.request(route(), json={})
        self.callback.assert_not_awaited()

    async def test_cache_error_does_not_trigger_send_retry(self):
        self.callback.side_effect = RuntimeError("disk full")
        self.assertIs(await self.http.request(route()), self.response)
        self.logger.exception.assert_called_once()
        self.http.send.assert_awaited_once()

    async def test_uploads_deletes_and_empty_responses_are_ignored(self):
        await self.http.request(route("/v2/groups/{group_openid}/files"))
        await self.http.request(route(method="DELETE"))
        self.http.send.return_value = None
        await self.http.request(route())
        self.http.send.return_value = {"code": 1}
        await self.http.request(route())
        self.callback.assert_not_awaited()

    async def test_concurrent_group_and_c2c_receipts_keep_their_payloads(self):
        targets = [route(), route("/v2/users/{openid}/messages")]
        payloads = [
            {"content": "group"},
            {"content": "private", "stream": {"state": 1}},
        ]
        await asyncio.gather(
            *(
                self.http.request(target, json=payload)
                for target, payload in zip(targets, payloads, strict=True)
            )
        )
        self.assertEqual(self.callback.await_count, 2)
        for target, payload in zip(targets, payloads, strict=True):
            self.callback.assert_any_await(self.http, target, payload, self.response)

    async def test_unload_disables_an_inflight_capture_and_restores_method(self):
        entered, resume = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await resume.wait()
            return self.response

        self.http.send.side_effect = send
        task = asyncio.create_task(self.http.request(route()))
        await entered.wait()
        self.observer.close()
        resume.set()
        self.assertIs(await task, self.response)
        self.assertIs(self.http_class.request, self.original)
        self.callback.assert_not_awaited()

    async def test_unload_preserves_later_wrapper_and_disables_own_callback(self):
        wrapped = self.http_class.request

        async def later(http, *args, **kwargs):
            return await wrapped(http, *args, **kwargs)

        self.http_class.request = later
        self.observer.close()
        self.assertIs(await self.http.request(route()), self.response)
        self.assertIs(self.http_class.request, later)
        self.callback.assert_not_awaited()
