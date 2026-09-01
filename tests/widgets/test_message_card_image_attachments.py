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

覆盖点
------
1. add_user_message 透传 ``_image_attachments``
2. normalize_message 白名单保留 ``_image_attachments``
3. set_image_attachments：本地路径 / 失效路径 + data URI 兜底 / 全部失效
4. _extract_image_uris 三种 image 块格式（image_url 字典 / 字符串 / image base64）
5. 工具注入块不影响兜底取序（前 N 块对齐附件数）
"""

import os
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication

from app.core.chat_session import ChatSession
from app.core.message_content import normalize_message
from app.widgets.message_card import MessageCard


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_card() -> MessageCard:
    _ensure_qapp()
    return MessageCard(role="user")


def _make_png(tmp_path, name: str = "img.png") -> str:
    """生成真实 PNG 文件（缩略图加载需要可解码数据）。"""
    path = os.path.join(str(tmp_path), name)
    pm = QPixmap(4, 4)
    pm.fill(Qt.red)
    assert pm.save(path, "PNG")
    return path


def _data_uri_of(path: str) -> str:
    import base64

    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


# ==================== 数据层：session 消息标记 ====================


def test_add_user_message_persists_image_attachments():
    s = ChatSession(name="t")
    s.add_user_message("看图", _image_attachments=["D:/a.png", "D:/b.png"])
    msg = s.messages[-1]
    assert msg["_image_attachments"] == ["D:/a.png", "D:/b.png"]


def test_add_user_message_skips_empty_attachments():
    s = ChatSession(name="t")
    s.add_user_message("纯文本", _image_attachments=[])
    msg = s.messages[-1]
    assert "_image_attachments" not in msg


def test_normalize_message_keeps_image_attachments():
    """白名单验证：字段缺失会被 consolidate_messages 剥掉，UI 恢复时无法回显。"""
    msg = normalize_message(
        {"role": "user", "content": "看图", "_image_attachments": ["D:/a.png"], "ts_ms": 1}
    )
    assert msg["_image_attachments"] == ["D:/a.png"]


def test_normalize_message_drops_invalid_attachments():
    msg = normalize_message({"role": "user", "content": "x", "_image_attachments": "not-a-list"})
    assert "_image_attachments" not in msg


# ==================== UI 层：缩略图条 ====================


def test_set_image_attachments_with_existing_paths(tmp_path):
    p1, p2 = _make_png(tmp_path, "a.png"), _make_png(tmp_path, "b.png")
    card = _make_card()
    card.set_image_attachments([p1, p2])
    assert card._image_strip.isVisible()
    # 2 张缩略图 + 末尾 stretch
    assert card._image_strip_lay.count() == 3


def test_set_image_attachments_fallback_to_data_uri(tmp_path):
    """路径失效 → 按序取 content 前 N 个 image 块 data URI 解码兜底。"""
    p = _make_png(tmp_path, "gone.png")
    uri = _data_uri_of(p)
    card = _make_card()
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": uri}},  # 附件块
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},  # 工具注入块
    ]
    card.set_image_attachments([os.path.join(str(tmp_path), "missing.png")], fallback_content=content)
    assert card._image_strip.isVisible()
    # 只取前 1 块（附件），工具注入块不显示
    assert card._image_strip_lay.count() == 2  # 1 缩略图 + stretch


def test_set_image_attachments_all_missing_no_fallback(tmp_path):
    card = _make_card()
    card.set_image_attachments([os.path.join(str(tmp_path), "missing.png")])
    assert not card._image_strip.isVisible()


def test_set_image_attachments_empty_or_invalid():
    card = _make_card()
    card.set_image_attachments(None)
    card.set_image_attachments([])
    card.set_image_attachments([None, "", 123])
    assert not card._image_strip.isVisible()


# ==================== image 块格式提取 ====================


def test_extract_image_uris_three_formats():
    uri = "data:image/png;base64,AAAA"
    content = [
        {"type": "text", "text": "x"},
        {"type": "image_url", "image_url": {"url": uri}},
        {"type": "input_image", "image_url": uri},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},  # 非 data URI 排除
    ]
    uris = MessageCard._extract_image_uris(content)
    assert len(uris) == 3
    assert all(u.startswith("data:image") for u in uris)


def test_extract_image_uris_non_list():
    assert MessageCard._extract_image_uris(None) == []
    assert MessageCard._extract_image_uris("text") == []
