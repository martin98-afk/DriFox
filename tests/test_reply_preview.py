# -*- coding: utf-8 -*-
"""通知正文预览清洗（app.utils.session_preview.get_reply_preview）单元测试。

回归锚点：回复以 ``<plan>`` 开头时，通知 toast 曾把「目标/路径/风险」当正文展示。
"""

from app.utils import session_preview

PLAN_LEAD = """<plan>
目标：收尾检查
路径：核对工作树与临时文件
风险：无
</plan>
已经把工作树和临时文件核对完了，没有残留。
"""


def test_plan_block_stripped_and_body_kept():
    preview = session_preview.get_reply_preview(PLAN_LEAD)
    assert "目标：" not in preview
    assert "风险：" not in preview
    assert preview.startswith("已经把工作树和临时文件核对完了")


def test_plan_only_returns_empty():
    """整条回复只有协议块时返回空串：plan 是内部计划，不该当通知正文。"""
    assert session_preview.get_reply_preview("<plan>\n目标：跑测试\n路径：pytest\n</plan>") == ""


def test_think_and_tool_blocks_stripped():
    text = "思考完了。\n<tool>\nname: shell\nsuccess: true\n</tool>\n结果在 README.md。"
    assert session_preview.get_reply_preview(text) == "思考完了。 结果在 README.md。"


def test_markdown_markers_removed():
    text = """## 结论

**已修复** *通知预览*，见 [文档](https://example.com)。

- 第一点
- 第二点

```
代码块内容不应出现
```

| 列 | 值 |
| --- | --- |
| a | b |
"""
    preview = session_preview.get_reply_preview(text)
    assert "#" not in preview
    assert "**" not in preview
    assert "https://example.com" not in preview
    assert "文档" in preview
    assert "代码块内容不应出现" not in preview
    assert "第一点" in preview and "第二点" in preview


def test_ask_block_stripped():
    text = "已完成。\n<ask>还要我继续吗？</ask>"
    assert session_preview.get_reply_preview(text) == "已完成。"


def test_inline_code_keeps_content_identifier_intact():
    """行内代码保留内容；``__init__`` 这类标识符不能被斜体规则吃掉下划线。"""
    assert session_preview.get_reply_preview("改为调用 `_do_post_stream_cleanup`。") == (
        "改为调用 _do_post_stream_cleanup。"
    )
    assert session_preview.get_reply_preview("见 __init__ 方法。") == "见 __init__ 方法。"


def test_clip_prefers_sentence_boundary():
    text = "第一句结束。第二句也结束了。" + "填充" * 30
    preview = session_preview.get_reply_preview(text, max_len=20)
    assert preview == "第一句结束。第二句也结束了。"
    assert len(preview) <= 20


def test_clip_hard_cut_adds_ellipsis():
    preview = session_preview.get_reply_preview("啊" * 100, max_len=10)
    assert preview == "啊" * 10 + "..."


def test_empty_content_returns_empty():
    assert session_preview.get_reply_preview("") == ""
    assert session_preview.get_reply_preview(None) == ""
    assert session_preview.get_reply_preview("   \n  ") == ""


def test_literal_plan_tag_inside_code_block_does_not_swallow_body():
    """代码块里字面写出的 <plan> 不能当协议块起点吞掉后面的正文。"""
    text = "示例：\n```\n<plan>\n```\n后面的正文必须保留。"
    preview = session_preview.get_reply_preview(text)
    assert "后面的正文必须保留。" in preview


def test_literal_plan_tag_inside_inline_code_keeps_following_text():
    """行内代码里的 <plan> 字面量同理：不能吞掉后面整段。"""
    text = "剥离 `<plan>` 等协议块后，通知正文就正常了。"
    preview = session_preview.get_reply_preview(text)
    assert preview == "剥离 <plan> 等协议块后，通知正文就正常了。"