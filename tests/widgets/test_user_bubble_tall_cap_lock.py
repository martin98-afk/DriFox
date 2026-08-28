# -*- coding: utf-8 -*-
"""回归：用户气泡 2 行短消息被 O(1) 快速路径锁死 (cap, MAX_HEIGHT)。

根因
----
PlainTextViewer._update_height 的"超高"单调缓存 _tall_cap 在**任何**撞限
（h >= MAX_HEIGHT）时无条件记录 cap。短消息收缩分支（按 naturalTextWidth
收缩 bubble_w）一旦测量异常（字体 fallback 未就绪 → naturalTextWidth 偏小
→ bubble_w 收缩到 ~80 → tiny 宽度下 2 行文本折出十几行 → h 撞 MAX_HEIGHT），
cap 即被记为"确认超高"。此后所有 ≤cap 的宽度永久走 O(1) 快速路径
setFixedSize(cap, MAX_HEIGHT)——2 行短消息显示为全宽 300 高、大片空白
（用户实测截图症状：内容一点点、气泡极高）。

修复
----
1. 撞限记录加条件 bubble_w >= cap（宽度用满上限仍超高 = 真·内容多才记录；
   收缩分支撞限是 tiny 宽度测量伪信号）。
2. 收缩分支撞限时回退全宽重测一次自愈（同时避免气泡收缩成 80px 孤条）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication  # noqa: F401  # 须先于 design_tokens 导入

from app.widgets.message_card import PlainTextViewer

_SHORT = "你看d:/work/DriFox/.drifox/plugins/context-stats 这个字体大小没有用ui上下文的"


def _flush(ms=120):
    qapp = QApplication.instance() or QApplication(sys.argv)
    loop = QTimer()
    loop.setSingleShot(True)
    loop.start(ms)
    while loop.isActive():
        qapp.processEvents()


def test_short_message_not_tall_capped():
    """短消息正常收缩：高度贴近内容，不触发 _tall_cap 记录。"""
    v = PlainTextViewer()
    v.set_width_cap(846)
    v.set_text(_SHORT)
    _flush()

    assert v.height() < 120, f"2 行短消息高度应贴近内容，实际 {v.height()}"
    assert v._tall_cap == 0, f"短消息不应记录超高缓存，实际 _tall_cap={v._tall_cap}"


def test_real_tall_content_still_capped():
    """真·内容超高（窄 cap 下长文本多行撞限）：缓存仍记录，快速路径保持有效。"""
    v = PlainTextViewer()
    # 大字体 + 窄 cap：宽度用满仍多行 → 真·超高
    v.text_edit.setStyleSheet(
        v.text_edit.styleSheet().replace("font-size: 18px", "font-size: 40px")
    )
    v.set_width_cap(360)
    v.set_text("这是一段用来验证超高缓存的超长用户消息。" * 12)
    _flush(qapp)

    assert v.height() == PlainTextViewer.MAX_HEIGHT, "真超高应撞 MAX_HEIGHT"
    assert v._tall_cap >= 360, f"真超高应记录缓存，实际 _tall_cap={v._tall_cap}"

    # 更窄宽度走 O(1) 快速路径（性能特性不回归）
    v.set_width_cap(300)
    _flush()
    assert v.width() == 300 and v.height() == PlainTextViewer.MAX_HEIGHT


def test_short_message_survives_narrow_then_wide():
    """窄→宽 resize 序列：短消息始终按内容收缩，不被锁死成 (cap, MAX_HEIGHT)。"""
    v = PlainTextViewer()
    for cap in (846, 400, 700, 846):
        v.set_width_cap(cap)
        if v._text != _SHORT:
            v.set_text(_SHORT)
        _flush()
        assert v.height() < 160, f"cap={cap} 时短消息高度异常: {v.height()}"
        assert v._tall_cap == 0, f"cap={cap} 时不应记录超高缓存"
