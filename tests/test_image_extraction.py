from pathlib import Path
from types import SimpleNamespace as NS

from astrbot_plugin_quote_cache.image_extraction import collect_images, qq_image_url


def collect(chain=(), raw=None, **kwargs):
    return collect_images(
        chain,
        raw,
        message_id="m1",
        sender_id="u1",
        sender_name="张三",
        timestamp=100,
        **kwargs,
    )


def test_official_extensionless_attachment():
    url = "https://multimedia.nt.qq.com.cn.evil.test/a"
    assert qq_image_url(url) is None
    assert qq_image_url("https://qpic.cn.evil.test/a") is None
    raw = {
        "attachments": [
            {
                "content_type": "image/jpeg",
                "url": "gchat.qpic.cn/download?appid=1&sig=a%2Bb",
            }
        ]
    }
    images = collect(raw=raw)
    assert images[0].ref == "https://gchat.qpic.cn/download?appid=1&sig=a%2Bb"
    assert not images[0].local_allowed


def test_received_local_image_does_not_redownload_top_level_attachment(tmp_path):
    chain = [NS(type="Image", file=str(tmp_path / "a.png"))]
    raw = {
        "attachments": [{"content_type": "image/png", "url": "https://gchat.qpic.cn/a"}]
    }
    images = collect(chain, raw)
    assert len(images) == 1 and images[0].local_allowed


def test_unrecognized_cdn_attachment_survives_partial_adapter_parsing(tmp_path):
    chain = [NS(type="Image", file=str(tmp_path / "a.png"))]
    raw = {
        "attachments": [
            {"content_type": "image/png", "url": "https://gchat.qpic.cn/a"},
            {"url": "https://multimedia.nt.qq.com.cn/download?rkey=other"},
        ]
    }
    images = collect(chain, raw)
    assert len(images) == 2
    assert images[1].ref.endswith("rkey=other")


def test_quoted_local_image_does_not_fetch_same_original_attachment(tmp_path):
    chain = [
        NS(
            type="Reply",
            id="quoted",
            chain=[NS(type="Image", file=str(tmp_path / "a.png"))],
        )
    ]
    raw = {
        "message_type": 103,
        "msg_elements": [
            {
                "id": "quoted",
                "attachments": [
                    {"content_type": "image/png", "url": "https://gchat.qpic.cn/a"}
                ],
            }
        ],
    }
    assert len(collect(chain, raw)) == 1


def test_quoted_and_forward_text_urls_keep_provenance():
    raw = {
        "message_type": 103,
        "msg_elements": [
            {
                "id": "quoted",
                "content": "看这个 https://gchat.qpic.cn/a?sig=abc",
                "attachments": [
                    {"contentType": "image/png", "url": "https://gchat.qpic.cn/b"}
                ],
            }
        ],
    }
    images = collect(raw=raw)
    assert len(images) == 2
    assert {image.message_id for image in images} == {"quoted"}
    assert {image.origin for image in images} == {"引用"}
    assert {image.sender_id for image in images} == {
        ""
    }  # Never attribute quoted images to the quoting user.


def test_nested_forward_preserves_node_and_actual_sender():
    raw = {
        "msg_elements": [
            {
                "id": "n1",
                "author": {"id": "author1", "username": "甲"},
                "content": "https://gchat.qpic.cn/a",
            },
            {"id": "n2", "content": "https://gchat.qpic.cn/b"},
        ]
    }
    images = collect(raw=raw)
    assert [image.origin for image in images] == ["转发/1", "转发/2"]
    assert images[0].sender_id == "author1" and images[1].sender_id == ""


def test_plain_qq_urls_only_and_dedupe():
    text = "https://gchat.qpic.cn/a?sig=xyz https://example.org/photo.png"
    images = collect([NS(type="Plain", text=text)], {"content": text})
    assert len(images) == 1
    assert images[0].ref.endswith("sig=xyz")
    assert collect([NS(type="Plain", text=text)], None) == []


def test_qq_nt_cdn_signed_link():
    url = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc&rkey=a%2Bb&spec=0"
    assert collect(raw={"content": url})[0].ref == url


def test_structured_reply_keeps_quote_provenance():
    image = NS(type="Image", url="https://gchat.qpic.cn/a", file="name.image")
    reply = NS(
        type="Reply",
        id="old",
        chain=[image],
        sender_id="author",
        sender_nickname="作者",
    )
    found = collect([reply])
    assert found[0].message_id == "old" and found[0].origin == "引用"


def test_traversal_and_image_limits():
    raw = {"content": " ".join(f"https://gchat.qpic.cn/{i}" for i in range(100))}
    assert len(collect(raw=raw, limit=3)) == 3
    circular = {}
    circular["data"] = circular
    assert collect(raw=circular) == []


def test_raw_payload_cannot_supply_local_paths():
    assert collect(raw={"type": "image", "data": {"file": "file:///etc/passwd"}}) == []


def test_qq_quote_follows_request_order_without_duplicates(tmp_path):
    # After PreProcess the chain holds local files, the QQ adapter puts the quote
    # first, and the quoted raw element carries no ID of its own.
    chain = [
        NS(
            type="Reply",
            id="q1",
            chain=[NS(type="Image", file=str(tmp_path / "q.png"))],
        ),
        NS(type="Plain", text="看"),
        NS(type="Image", file=str(tmp_path / "c.png")),
    ]
    raw = {
        "id": "m1",
        "message_type": 103,
        "attachments": [{"content_type": "image/png", "url": "gchat.qpic.cn/c"}],
        "msg_elements": [
            {
                "content": "https://gchat.qpic.cn/text",
                "attachments": [
                    {"content_type": "image/png", "url": "gchat.qpic.cn/q"}
                ],
            }
        ],
    }
    found = [(Path(i.ref).name, i.message_id, i.origin) for i in collect(chain, raw)]
    assert found == [
        ("c.png", "m1", "当前消息"),
        ("q.png", "q1", "引用"),
        ("text", "q1", "引用"),
    ]
    quote_only = collect(chain[:2], {**raw, "attachments": []})
    assert [Path(i.ref).name for i in quote_only] == ["q.png", "text"]
