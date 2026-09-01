# -*- coding: utf-8 -*-
"""回归测试：用户消息图片附件在气泡上方预览（发送 + 恢复两路）。

功能链路
--------
- 发送时：main_widget 把附件图片路径传给卡片（set_image_attachments），
  同时经 engine → chat_session.add_user_message 以 ``_image_attachments``
  消息级标记写入 session
- 恢复会话时：normalize_message 必须保留 ``_image_attachments``（否则被
  consolidate_messages 白名单剥掉）；卡片渲染时路径失效则从 content 的
  image 块按序解码 data URI 兜底（附件块在前、工具注入块在后，顺序可靠）

测试范围
-------
- 数据层：add_user_message 标记透传、normalize_message 白名单保留
- 决策层：plan_image_attachment_sources / extract_image_data_uris 纯函数
- UI 薄壳（缩略图构建、addWidget、点击放大）为 Qt 侧人工验证——pytest +
  offscreen 环境下 QPixmap 像素操作触发 native crash（环境固有问题）
"""

import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from app.core.chat_session import ChatSession
from app.core.message_content import normalize_message
from app.widgets.message_card import extract_image_data_uris, plan_image_attachment_sources


def _data_uri_png() -> str:
    """1x1 红色 PNG 的 data URI（固定字节，无需生成文件）"""
    return (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


# ==================== 数据层：session 消息标记 ====================


def test_add_user_message_persists_image_attachments():
    s = ChatSession(name="t")
    s.add_user_message("看图", _image_attachments=["D:/a.png", "D:/b.png"])
    assert s.messages[-1]["_image_attachments"] == ["D:/a.png", "D:/b.png"]


def test_add_user_message_skips_empty_attachments():
    s = ChatSession(name="t")
    s.add_user_message("纯文本", _image_attachments=[])
    assert "_image_attachments" not in s.messages[-1]


def test_normalize_message_keeps_image_attachments():
    """白名单验证：字段缺失会被 consolidate_messages 剥掉，UI 恢复时无法回显。"""
    msg = normalize_message(
        {"role": "user", "content": "看图", "_image_attachments": ["D:/a.png"], "ts_ms": 1}
    )
    assert msg["_image_attachments"] == ["D:/a.png"]


def test_normalize_message_drops_invalid_attachments():
    msg = normalize_message({"role": "user", "content": "x", "_image_attachments": "not-a-list"})
    assert "_image_attachments" not in msg


# ==================== 决策层：渲染来源规划 ====================


def test_plan_existing_path_uses_file(tmp_path):
    """路径存在（发送当下的常态）→ 缩略图走本地文件加载"""
    p = os.path.join(str(tmp_path), "a.png")
    with open(p, "wb") as f:
        f.write(b"fake")
    assert plan_image_attachment_sources([p]) == [(p, None, p)]


def test_plan_missing_path_falls_back_in_order():
    """路径失效（恢复会话 temp 被清理）→ 按序取 content 前 N 个 image 块兜底。

    附件块在前、工具注入块在后追加：N=1 时只取序 0（附件），
    序 1 的注入块不得被消费。
    """
    uri = _data_uri_png()
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": uri}},  # 附件块（序 0）
        {"type": "image_url", "image_url": {"url": uri}},  # 工具注入块（序 1）
    ]
    plan = plan_image_attachment_sources(["D:/missing.png"], fallback_content=content)
    assert plan == [(None, uri, "D:/missing.png")]


def test_plan_mixed_existing_and_fallback():
    """两张附件一张存活一张失效 → 各走各的来源"""
    p = os.path.join(str(tmp_path_d()), "alive.png")
    with open(p, "wb") as f:
        f.write(b"fake")
    uri = _data_uri_png()
    content = [{"type": "image_url", "image_url": {"url": uri}}]
    plan = plan_image_attachment_sources(["D:/gone.png", p], fallback_content=content)
    assert plan == [(None, uri, "D:/gone.png"), (p, None, p)]


def tmp_path_d():
    import tempfile

    return tempfile.mkdtemp()


def test_plan_all_missing_no_fallback():
    assert plan_image_attachment_sources(["D:/missing.png"]) == []


def test_plan_invalid_input():
    assert plan_image_attachment_sources(None) == []
    assert plan_image_attachment_sources([]) == []
    assert plan_image_attachment_sources([None, "", 123]) == []


# ==================== image 块格式提取 ====================


def test_extract_image_uris_three_formats():
    uri = _data_uri_png()
    content = [
        {"type": "text", "text": "x"},
        {"type": "image_url", "image_url": {"url": uri}},
        {"type": "input_image", "image_url": uri},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},  # 非 data URI 排除
    ]
    uris = extract_image_data_uris(content)
    assert len(uris) == 3
    assert all(u.startswith("data:image") for u in uris)


def test_extract_image_uris_non_list():
    assert extract_image_data_uris(None) == []
    assert extract_image_data_uris("text") == []
