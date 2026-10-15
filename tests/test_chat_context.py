from collections import deque
from types import SimpleNamespace as NS

# AstrBot 4.29 内置群聊上下文的注入格式
HEADER = (
    "<system_reminder>You are in a group chat. "
    "Belows are group chat context after your last reply:\n--- BEGIN CONTEXT---\n"
)
FOOTER = "\n--- END CONTEXT ---\n</system_reminder>"


def block(*records):
    return HEADER + "\n".join(records) + FOOTER


def slots(own=(), quote=(), other=()):
    return {"own": list(own), "quote": list(quote), "other": list(other)}


def test_line_ids_follow_image_order(chat_module):
    line = "[张三/14:02:31]:  第一张 [Image] 第二张 [Image]"
    assert chat_module.annotate_line(line, slots(["img_a", "img_b"])) == (
        "[张三/14:02:31]:  第一张 [Image img_a] 第二张 [Image img_b]"
    )
    # 下载失败的那张保持原样，其余位置不变
    assert chat_module.annotate_line(line, slots([None, "img_b"])) == (
        "[张三/14:02:31]:  第一张 [Image] 第二张 [Image img_b]"
    )
    assert chat_module.annotate_line(line, slots([None, None])) is None


def test_ambiguous_lines_get_ids_at_the_end(chat_module):
    # 图片描述、引用里的图、数量不一致时都不原地替换
    captioned = "[张三/14:02:31]:  [Image: 一道电路题]"
    assert chat_module.annotate_line(captioned, slots(["img_a"])) == (
        captioned + " 〔本条图片 img_a〕"
    )
    quoted = "[李四/14:03:10]:  [Quote(张三: 看这个[Image])] 我也不会 [Image]"
    assert chat_module.annotate_line(quoted, slots(["img_b"], ["img_a"])) == (
        quoted + " 〔本条图片 img_b；引用中的图片 img_a〕"
    )
    lost = "[张三/14:02:31]:  [Image]"
    assert chat_module.annotate_line(lost, slots(["img_a", None])) == (
        lost + " 〔本条图片 img_a（另 1 张未缓存）〕"
    )
    assert chat_module.annotate_line("[张三/1]:  看", slots(other=["img_x"])) == (
        "[张三/1]:  看 〔其他图片 img_x〕"
    )


def test_annotator_rewrites_only_injected_lines(chat_module):
    annotator = chat_module.GroupContextAnnotator()
    first = "[张三/14:02:31]:  这题第二问咋做 [Image]"
    second = "[李四/14:03:10]:  我也不会"
    current = "[雲過河洛_/14:05:00]: ⚠️[DIRECTED AT YOU]  [At: 云云] 看图 [Image]"
    annotator.remember("umo", "r1", first, slots(["img_a"]))
    annotator.remember("umo", "r3", current, slots(["img_c"]))
    annotator.remember("other", "r9", first, slots(["img_z"]))
    reminder = chat_module.TextPart(
        text="<system_reminder>User ID: 1</system_reminder>"
    )
    parts = [reminder, chat_module.TextPart(text=block(first, second))]

    assert annotator.annotate("umo", "r3", parts)
    assert parts[0] is reminder
    assert parts[1].text == block(
        "[张三/14:02:31]:  这题第二问咋做 [Image img_a]", second
    )
    assert parts[-1].text.startswith(chat_module.NOTE_PREFIX)
    # 用过的行和当前消息的行都会移除，其他会话不受影响
    assert "umo" not in annotator.pending
    assert list(annotator.pending["other"]) == ["r9"]
    # 不在注入记录里的行不会被凭空加进去
    untouched = [chat_module.TextPart(text=block(second))]
    assert not annotator.annotate("other", None, untouched)
    assert untouched[0].text == block(second) and len(untouched) == 1


def test_annotator_matches_whole_lines_only(chat_module):
    annotator = chat_module.GroupContextAnnotator()
    short = "[张三/14:02:31]:  [Image]"
    annotator.remember("umo", "r1", short, slots(["img_a"]))
    longer = short + " 后面还有字"
    parts = [{"type": "text", "text": block(longer)}]
    assert not annotator.annotate("umo", None, parts)
    assert parts[0]["text"] == block(longer)


def test_builtin_record_reads_exact_line(chat_module):
    recorder = NS(
        raw_records={"umo": deque(["a", "b"])},
        _record_ids={"umo": deque(["r1", "r2"])},
    )
    context = NS(
        get_registered_star=lambda name: NS(star_cls=NS(group_chat_context=recorder))
    )
    event = NS(
        unified_msg_origin="umo",
        get_extra={"_group_context_record_id": "r2"}.get,
    )
    assert chat_module.builtin_record(context, event) == ("r2", "b")
    # 记录已被消费、旧版 AstrBot 没有该结构时都安静地放弃
    recorder._record_ids["umo"].clear()
    assert chat_module.builtin_record(context, event) is None
    assert chat_module.builtin_record(NS(), event) is None
    no_record = NS(unified_msg_origin="umo", get_extra={}.get)
    assert chat_module.builtin_record(context, no_record) is None


def test_transcript_names_sender_time_quote_and_images(chat_module):
    def message(sender, content, timestamp):
        return NS(sender_name=sender, content=content, timestamp=timestamp)

    asked = message("张三", "这题第二问咋做 [图片]", 1_792_000_000)
    reply = message("李四", "[引用消息] 我也不会", 1_792_000_060)
    text = chat_module.render_transcript(
        [
            (asked, ["img_a"], None),
            (reply, [], (asked, ["img_a"])),
            (message("王五", "[图片] [图片]", 1_792_000_090), ["img_b"], None),
        ],
        omitted=3,
        content_chars=200,
    )
    assert text.startswith(chat_module.TRANSCRIPT_PREFIX)
    lines = text.splitlines()
    assert "更早还有 3 条未列出" in lines[2]
    assert lines[3].endswith("] 张三：这题第二问咋做 [图片 img_a]")
    assert (
        "李四：[引用 张三 " in lines[4]
        and "「这题第二问咋做 [图片 img_a]」" in lines[4]
    )
    assert lines[5].endswith("王五：[图片] [图片] 〔图片 img_b〕")
