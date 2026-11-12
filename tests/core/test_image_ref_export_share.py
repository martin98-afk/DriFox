# -*- coding: utf-8 -*-
"""T-img image_ref 丢图修复测试：导出 md / 分享卡占位 + API 防御链一致性。

覆盖三条断言面：
1. export_messages_to_markdown 对含 image_ref 的 user 消息输出 [图片] 占位
2. share_card._content_to_plain 对 image_ref 输出 [图片] 占位
3. _extract_content_for_api 的 has_image 判定与 _strip_image_blocks 识别一致
   （非视觉模型下不残留 image_ref 块，防死循环）

纯函数级，无 UI、无 Qt 依赖（share_card 导入做 Qt 环境跳过处理）。
"""

from __future__ import annotations

import pytest

from app.core.conversation.message_content import (
    _extract_content_for_api,
    _strip_image_blocks,
    ensure_content_blocks,
)

IMAGE_REF_BLOCK = {
    "type": "image_ref",
    "image_ref": {
        "file": "abc123.png",
        "meta": "image/png;base64",
        "session_id": "sess1",
    },
}


def _user_msg_with_image_ref() -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "看看这张截图"},
            dict(IMAGE_REF_BLOCK),
        ],
    }


def test_ensure_content_blocks_keeps_image_ref():
    """ensure_content_blocks 对 image_ref 透传（不再落入纯文本 else 分支丢弃）。"""
    blocks = ensure_content_blocks([dict(IMAGE_REF_BLOCK)])
    assert any(b.get("type") == "image_ref" for b in blocks), blocks


def test_export_markdown_contains_placeholder():
    """导出 md：含 image_ref 的 user 消息输出含 [图片]，不静默丢图。"""
    from app.widgets.ui_helpers import export_messages_to_markdown

    md = export_messages_to_markdown([_user_msg_with_image_ref()])
    assert "[图片]" in md
    assert "看看这张截图" in md


def test_share_card_plain_contains_placeholder():
    """分享卡纯文本：image_ref 输出 [图片] 占位。"""
    pytest.importorskip("PySide6")
    from app.widgets.cards.floating.share_card import _content_to_plain

    plain = _content_to_plain(_user_msg_with_image_ref()["content"])
    assert "[图片]" in plain
    assert "看看这张截图" in plain


def test_has_image_and_strip_consistent_for_image_ref():
    """has_image 判定与 _strip_image_blocks 识别一致：非视觉模型下
    返回纯文本且不含 image_ref 字典残留（防 strip 不认导致内容残留）。"""
    content = [
        {"type": "text", "text": "正文"},
        dict(IMAGE_REF_BLOCK),
    ]
    out = _extract_content_for_api(content, supports_vision=False)
    assert isinstance(out, str), "含 image_ref 时非视觉模型应返回纯文本"
    assert "[图片]" in out
    assert "正文" in out

    stripped = _strip_image_blocks(content)
    assert stripped == "正文\n[图片]"


def test_vision_model_keeps_image_ref_block():
    """视觉模型下 image_ref 块原样保留在 multimodal list。"""
    content = [dict(IMAGE_REF_BLOCK)]
    out = _extract_content_for_api(content, supports_vision=True)
    assert isinstance(out, list)
    assert any(b.get("type") == "image_ref" for b in out)
