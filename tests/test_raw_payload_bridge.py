import unittest

from astrbot_plugin_quote_cache.raw_payload_bridge import RawPayloadBridge


class Message:
    def __init__(self, message_id):
        self.id = message_id


class RawPayloadBridgeTests(unittest.TestCase):
    def test_capture_and_take(self):
        bridge = RawPayloadBridge(max_entries=100, ttl_seconds=60)
        payload = {
            "id": "QQ-1",
            "message_type": 103,
            "message_scene": {"ext": ["ref_msg_idx=REFIDX-1"]},
        }
        bridge.capture(payload)
        payload["message_type"] = 0
        captured = bridge.take(Message("QQ-1"))
        self.assertEqual(captured["message_type"], 103)
        self.assertEqual(bridge.pending, 0)

    def test_unknown_message_returns_none(self):
        bridge = RawPayloadBridge(max_entries=100, ttl_seconds=60)
        self.assertIsNone(bridge.take(Message("missing")))


if __name__ == "__main__":
    unittest.main()
