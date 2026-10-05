import unittest
from enum import Enum
from types import SimpleNamespace

from astrbot_plugin_quote_cache.message_codec import (
    extract_group_name,
    history_outline,
    message_aliases,
    message_identifiers,
    sent_message_outline,
)


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
    def test_qq_indices_preserve_padding_and_separate_target(self):
        message = SimpleNamespace(
            message_id="ROBOT1.0_current",
            raw_message=SimpleNamespace(
                id="ROBOT1.0_current",
                raw_data={
                    "id": "ROBOT1.0_current",
                    "message_scene": {
                        "ext": [
                            "msg_idx=REFIDX_own==",
                            "ref_msg_idx=REFIDX_target=",
                            "auth_token=secret",
                        ]
                    },
                    "msg_elements": [{"msg_idx": "REFIDX_nested"}],
                },
            ),
        )
        self.assertEqual(
            message_identifiers(message),
            (
                "ROBOT1.0_current",
                "REFIDX_own==",
                "REFIDX_target=",
            ),
        )

    def test_native_id_is_not_lost_when_astrbot_uses_same_id(self):
        self.assertEqual(
            message_identifiers(
                {
                    "message_id": "same",
                    "raw_message": {"id": "same"},
                }
            ),
            ("same", "", ""),
        )

    def test_missing_scene_does_not_use_quoted_message_identity(self):
        self.assertEqual(
            message_identifiers(
                {
                    "message_id": "current",
                    "raw_message": {
                        "message_reference": {"message_id": "target"},
                        "msg_elements": [{"msg_idx": "REFIDX_target"}],
                    },
                }
            ),
            ("current", "", ""),
        )

    def test_sent_outline_does_not_store_upload_or_auth_tokens(self):
        content, components = sent_message_outline(
            {
                "markdown": {"content": "回复正文"},
                "media": {"file_info": "secret-upload-token"},
                "msg_id": "incoming-not-outgoing-id",
            }
        )
        self.assertEqual(content, "回复正文 [富媒体]")
        self.assertNotIn("secret", repr(components))

    def test_group_name_prefers_standard_astrbot_group(self):
        message_obj = type(
            "Message",
            (),
            {
                "group": type("Group", (), {"group_name": "云云的猫窝"})(),
                "raw_message": {"group_name": "原始群名"},
            },
        )()
        self.assertEqual(extract_group_name(message_obj), "云云的猫窝")

    def test_group_name_can_use_adapter_raw_payload(self):
        message_obj = {"raw_message": {"channel": {"name": "闲聊频道"}}}
        self.assertEqual(extract_group_name(message_obj), "闲聊频道")

    def test_group_name_is_empty_when_official_payload_only_has_openid(self):
        message_obj = {
            "group": {"group_name": None},
            "raw_message": {"group_openid": "opaque-openid"},
        }
        self.assertEqual(extract_group_name(message_obj), "")

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
