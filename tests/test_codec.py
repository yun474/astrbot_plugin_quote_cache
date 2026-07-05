from dataclasses import dataclass
import unittest

from astrbot_plugin_quote_cache.message_codec import (
    current_aliases,
    embedded_quote,
    parse_scene_ext,
    reference_aliases,
    serialize_chain,
)


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, url):
        self.url = url
        self.file = url


class Reply:
    def __init__(self, id, chain=None):
        self.id = id
        self.chain = chain or []
        self.message_str = ""
        self.sender_id = "u1"
        self.sender_nickname = "tester"
        self.time = 123


@dataclass
class Obj:
    message_id: str
    raw_message: object
    message: list


class Event:
    def __init__(self, raw, chain):
        self.message_obj = Obj("ASTR-1", raw, chain)
        self._extra = {}

    def get_extra(self, key=None, default=None):
        return self._extra if key is None else self._extra.get(key, default)


class CodecTests(unittest.TestCase):
    def test_parse_ext_and_aliases(self):
        raw = {
            "id": "QQ-1",
            "message_scene": {
                "ext": ["ref_msg_idx=REFIDX-old", "msg_idx=REFIDX-new"]
            },
        }
        event = Event(raw, [Reply("ASTR-old"), Plain("hello")])
        self.assertEqual(
            parse_scene_ext(raw["message_scene"]["ext"]),
            ("REFIDX-old", "REFIDX-new"),
        )
        self.assertEqual(dict(current_aliases(event))["ASTR-1"], "astr_message_id")
        self.assertIn("QQ-1", dict(current_aliases(event)))
        self.assertIn("REFIDX-new", dict(current_aliases(event)))
        self.assertEqual(reference_aliases(event), ["ASTR-old", "REFIDX-old"])

    def test_quote_103_embedded(self):
        raw = {
            "message_type": 103,
            "message_scene": {"ext": ["ref_msg_idx=REFIDX-x"]},
            "msg_elements": [
                {
                    "msg_idx": "REFIDX-x",
                    "content": "quoted text",
                    "attachments": [
                        {
                            "content_type": "image/png",
                            "url": "https://example.com/a.png",
                            "filename": "a.png",
                        }
                    ],
                }
            ],
        }
        data = embedded_quote(Event(raw, []))
        self.assertEqual(data["content"], "quoted text")
        self.assertEqual(data["ref_index"], "REFIDX-x")
        self.assertEqual(data["attachments"][0]["type"], "image")

    def test_chain_supports_text_and_media(self):
        components, attachments, outline = serialize_chain(
            [Plain("hello"), Image("https://example.com/a.png")]
        )
        self.assertEqual(components[0]["text"], "hello")
        self.assertEqual(attachments[0]["type"], "image")
        self.assertIn("hello", outline)
        self.assertIn("[image:", outline)


if __name__ == "__main__":
    unittest.main()
