# -*- coding: utf-8 -*-
"""回归测试：用户气泡在窗口 resize 后宽度/高度跟随（Bug: 文字跑出显示范围）。

根因
----
窗口 resize 全程 MessageCard 处于 `_resize_preview_mode=True`：
  1. resizeEvent → set_resize_preview_mode(True)（含 user 卡片，仅设标志位）
  2. 防抖 sync_width(target_width=新宽度) → user 分支的
     `if not self._resize_preview_mode` 守卫把 viewer.set_width_cap() 跳过
  3. resize 结束 _sync_all_cards_width 分批退出 preview →
     set_resize_preview_mode(False) 对 user 卡片直接 return（不补同步）
→ 气泡 cap/宽度/高度停留在 resize 前的旧值；窗口缩小后固定尺寸的
  PlainTextViewer 超出可视区，文字跑到显示范围之外。

而该守卫本意是保护 CodeWebViewer（WebEngine 重排昂贵），PlainTextViewer
轻量无需 placeholder 优化（set_resize_preview_mode 注释亦如此声明），
两处逻辑自相矛盾。

修复（message_card.py sync_width user 分支）：PlainTextViewer 的
set_width_cap 不受 preview 模式拦截，resize 期间照常注入宽度上限。
"""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from app.widgets.message_card import MessageCard, PlainTextViewer

_LONG_TEXT = (
    "这是一条比较长的用户消息，用来测试气泡在宽度变化时高度是否能自动跟随。"
    "当窗口缩小后，可用文字宽度变窄，文本应该换行成更多行，气泡高度应当相应增加。"
    "如果高度没有跟着增加，底部的部分文字就会被裁剪到显示范围之外，这就是本次要排查的 bug。"
    "再补一点内容让它超过三行，确保走用满上限拉宽的分支："
    "补充内容补充内容补充内容补充内容补充内容补充内容补充内容补充内容。"
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _flush(qapp, ms=80):
    """跑事件循环让 10ms singleShot(_schedule_update_height) 等定时器落地。"""
    loop = QTimer()
    loop.setSingleShot(True)
    loop.start(ms)
    while loop.isActive():
        qapp.processEvents()


def _make_user_card(qapp) -> MessageCard:
    card = MessageCard(role="user")
    card.viewer.set_text(_LONG_TEXT)
    _flush(qapp)
    return card


def test_sync_width_updates_cap_during_preview_mode(qapp):
    """resize 全程（preview 模式开启期间）sync_width 必须同步 viewer 宽度上限。

    复现真实时序：preview(True) → sync_width(小宽度) → preview(False)。
    """
    card = _make_user_card(qapp)
    viewer: PlainTextViewer = card.viewer

    # resize 开始：preview 模式开启（真实链路 _set_cards_resize_preview_mode）
    card.set_resize_preview_mode(True)
    # resize 期间防抖同步：窗口 900 → 400
    card.sync_width(target_width=900)
    card.sync_width(target_width=400)
    # resize 结束：分批退出 preview
    card.set_resize_preview_mode(False)

    assert viewer._width_cap == 400 - 24, (
        f"preview 模式不应拦截 user 气泡 set_width_cap，期望 cap=376，实际 {viewer._width_cap}"
    )


def test_bubble_shrinks_and_grows_with_cap(qapp):
    """cap 缩小→气泡变窄变高；cap 放大→气泡变宽变矮（高度双向跟随）。"""
    viewer = PlainTextViewer()
    viewer.set_text(_LONG_TEXT)
    viewer.set_width_cap(676)
    _flush(qapp)
    w_wide, h_wide = viewer.width(), viewer.height()

    viewer.set_width_cap(276)
    _flush(qapp)
    w_narrow, h_narrow = viewer.width(), viewer.height()
    assert w_narrow < w_wide, "cap 缩小后气泡宽度应收缩"
    assert h_narrow > h_wide, "cap 缩小后换行变多，高度应增加"

    viewer.set_width_cap(676)
    _flush(qapp)
    assert viewer.height() < h_narrow, "cap 放大后高度应回落"
    assert viewer.width() > w_narrow, "cap 放大后宽度应恢复"


def test_no_clip_at_fixed_size(qapp):
    """固定尺寸下 document 实际需求高度不得超过 viewer 高度（无裁剪）。"""
    viewer = PlainTextViewer()
    viewer.set_text(_LONG_TEXT)
    for cap in (676, 476, 276):
        viewer.set_width_cap(cap)
        _flush(qapp)
        need = viewer.text_edit.document().size().height() + 12
        assert viewer.height() >= need - 1, (
            f"cap={cap}: viewer_h={viewer.height()} 低于内容需求 {need:.0f}，底部文字会被裁剪"
        )
