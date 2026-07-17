import unittest
from enum import Enum

from astrbot_plugin_quote_cache.message_codec import history_outline, message_aliases


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, url):
        self.url = url


class Record:
    pass


class File:
    def __init__(self, name):
        self.name = name


class Reply:
    def __init__(self, message_str):
        self.message_str = message_str


class ComponentType(Enum):
    Image = "Image"


class CodecTests(unittest.TestCase):
    def test_history_outline_uses_media_placeholders_without_urls(self):
        content, components = history_outline(
            [
                Plain("看看这个"),
                Image("https://example.invalid/secret.jpg"),
                Record(),
                File("说明.txt"),
                Reply("不应重复写入被引用正文"),
            ]
        )
        self.assertEqual(
            content,
            "看看这个 [图片] [语音] [文件:说明.txt] [引用消息]",
        )
        serialized = repr(components)
        self.assertNotIn("example.invalid", serialized)
        self.assertNotIn("不应重复写入", serialized)

    def test_fallback_text_is_used_when_chain_has_no_plain_component(self):
        content, _ = history_outline([Image("ignored")], "平台提供的正文")
        self.assertEqual(content, "平台提供的正文 [图片]")

    def test_dict_component_accepts_enum_type(self):
        content, _ = history_outline([{"type": ComponentType.Image}])
        self.assertEqual(content, "[图片]")

    def test_message_aliases_remain_compatible_with_old_schema(self):
        message_obj = type(
            "Message",
            (),
            {
                "message_id": "astr-1",
                "raw_message": {"id": "raw-1", "message_id": "raw-2"},
            },
        )()
        self.assertEqual(
            dict(message_aliases(message_obj)),
            {
                "astr-1": "astr_message_id",
                "raw-1": "raw_id",
                "raw-2": "raw_message_id",
            },
        )


if __name__ == "__main__":
    unittest.main()
