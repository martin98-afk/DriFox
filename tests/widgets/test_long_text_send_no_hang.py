# -*- coding: utf-8 -*-
"""回归：超长文本发送时主线程被 QTextDocument 全量布局钉死（软件卡死）。

现象
----
粘贴/发送几万到几十万字符的文本，整个软件冻住数秒，输入框和所有 UI 都不响应。

根因（offscreen 实测）
----
两处独立的全量布局，都随字符数**超线性**增长（不是线性，随 block 数 × 字符数）：

1. 输入框 ``SendableTextEdit._adjust_height_to_content``
   每次 textChanged 调 ``document().size().height()``，强制 QTextDocument 对
   整篇做一次全量布局：
       10 万字符   183 ms
       30 万字符  1458 ms
       50 万字符  4556 ms
2. 用户气泡 ``PlainTextViewer._update_height``
   发送时 MessageCard.update_content → finish_streaming 发生在
   ``_add_chat_widget → _sync_single_card_width`` 之前，此时 ``_width_cap`` 还是
   初始 16777215（未注入），原有 ``cap < 100000`` 的 O(1) 快速路径拦不到，
   落到独立测量文档的全量测量：
       10 万字符   167 ms
       50 万字符  4362 ms

修复
----
两处都加 O(1) 判据（内容必然超过高度上限时不再问布局）：
- 输入框：``blockCount > 20``（20 行 × 15px = 300px）或 ``characterCount >= 15000``
  （最乐观排版——4000px 宽、单字符 5.25px——也需 15 行）→ 直接钉 300 高。
- 用户气泡：``len(_text) >= _definitely_tall_chars()``（既有阈值 8928）在
  cap 未注入的分支同样生效 → 直接钉 MAX_HEIGHT。

修复后实测
----
- 输入框 setPlainText 50 万：4579 ms → 19.65 ms
- 用户气泡 finish_streaming 50 万：4175 ms → 0.08 ms
"""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DRIFOX_NO_CODEBUDDY_REFRESH", "1")

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from PyQt5.QtWidgets import QApplication  # noqa: E402

# 卡死阈值：单次操作超过 500ms 即用户可感知的冻结。留足离屏与真机的余量，
# 只拦「数量级回归」（原实现 4 秒级），不拦正常波动。
_HANG_MS = 500.0

# 50 万字符：真机复现卡死的量级，离屏修复后两处都 <25ms
_HUGE_CHARS = 500_000


def _qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_text(n_chars: int, line_len: int = 80) -> str:
    """构造近似真实的长文本（定长行 + 换行），保证 blockCount 与字符数双高"""
    unit = "这是一段用于测试发送卡顿的超长文本内容 abcdefghij"
    lines, total = [], 0
    while total < n_chars:
        lines.append((unit * (line_len // len(unit) + 2))[:line_len])
        total += line_len + 1
    return "\n".join(lines)


def _timed(fn):
    import time

    t0 = time.perf_counter()
    r = fn()
    return r, (time.perf_counter() - t0) * 1000


class TestInputAreaTallTextFastPath:
    """输入框：超长文本不再触发全量文档布局"""

    def test_set_plain_text_huge_not_blocking(self):
        from app.widgets.bottom_input_area import SendableTextEdit

        _qapp()
        box = SendableTextEdit()
        box.resize(900, 120)
        box._initializing = False
        box.show()
        _qapp().processEvents()
        try:
            text = _make_text(_HUGE_CHARS)
            _, dt = _timed(lambda: box.setPlainText(text))
            assert dt < _HANG_MS, (
                f"输入框 setPlainText {_HUGE_CHARS} 字符耗时 {dt:.1f} ms ≥ {_HANG_MS} ms，"
                "说明 _adjust_height_to_content 又对整篇做了全量布局"
            )
        finally:
            box.close()

    def test_height_pinned_to_max_for_tall_text(self):
        """高度仍须钉到上限（不能因为跳过布局就不撑高/不滚动）"""
        from app.widgets.bottom_input_area import SendableTextEdit

        _qapp()
        box = SendableTextEdit()
        box.resize(900, 120)
        box._initializing = False
        box.show()
        _qapp().processEvents()
        try:
            box.setPlainText(_make_text(_HUGE_CHARS))
            assert box.height() == box.maximumHeight() == 300
            # 内容确实可滚动（有真实余量，不是假滚条）
            assert box._needs_input_scrollbar()
        finally:
            box.close()

    def test_short_text_still_measured_exactly(self):
        """短文本必须走真实测量：高度随行数增长，不能一律钉 300"""
        from app.widgets.bottom_input_area import SendableTextEdit

        _qapp()
        box = SendableTextEdit()
        box.resize(900, 120)
        box._initializing = False
        box.show()
        _qapp().processEvents()
        try:
            box.setPlainText("单行")
            h1 = box.height()
            box.setPlainText("a\nb\nc\nd")
            h4 = box.height()
            assert h1 < h4 < 300, f"短文本高度未被真实测量：1 行={h1} 4 行={h4}"
        finally:
            box.close()

    def test_threshold_not_triggered_by_medium_text(self):
        """阈值不能过低：20 行以内内容须按真实高度收缩，不许提前钉满 300"""
        from app.widgets.bottom_input_area import SendableTextEdit

        _qapp()
        box = SendableTextEdit()
        box.resize(900, 120)
        box._initializing = False
        box.show()
        _qapp().processEvents()
        try:
            box.setPlainText("\n".join(f"第{i}行" for i in range(5)))
            assert box.height() < 300, f"5 行文本被误钉到满高：{box.height()}"
        finally:
            box.close()


class TestUserBubbleTallTextFastPath:
    """用户气泡：cap 未注入时也不许做全量测量"""

    def test_finish_streaming_huge_not_blocking(self):
        from app.widgets.message_card import MessageCard

        _qapp()
        card = MessageCard(parent=None, role="user")
        card.resize(900, 400)
        card.show()
        _qapp().processEvents()
        try:
            card.update_content(_make_text(_HUGE_CHARS))
            _, dt = _timed(card.finish_streaming)
            assert dt < _HANG_MS, (
                f"用户卡 finish_streaming {_HUGE_CHARS} 字符耗时 {dt:.1f} ms ≥ {_HANG_MS} ms，"
                "说明 _update_height 在 cap 未注入时又走了全量文档测量"
            )
        finally:
            card.close()

    def test_bubble_height_capped(self):
        """超长内容仍须定型在 MAX_HEIGHT（内容靠内滚查看）"""
        from app.widgets.card_viewers import PlainTextViewer

        _qapp()
        v = PlainTextViewer()
        v.show()
        _qapp().processEvents()
        try:
            # 不调 set_width_cap：复刻 cap 未注入的真实时序
            v.set_text(_make_text(_HUGE_CHARS))
            assert v.height() == v.MAX_HEIGHT
        finally:
            v.cleanup()
            v.close()

    def test_width_cap_applied_after_tall_path(self):
        """跳过测量后，宽度仍须由随后的 set_width_cap 正确注入"""
        from app.widgets.card_viewers import PlainTextViewer

        _qapp()
        v = PlainTextViewer()
        v.show()
        _qapp().processEvents()
        try:
            v.set_text(_make_text(_HUGE_CHARS))
            v.set_width_cap(760)
            assert v.width() == 760
            assert v.height() == v.MAX_HEIGHT
            assert v._tall_cap == 760, "宽度到上限后应记入超高单调缓存，后续 resize 走 O(1)"
        finally:
            v.cleanup()
            v.close()

    def test_short_text_still_measured_exactly(self):
        """短文本不许被超长快速路径波及：仍按内容收缩"""
        from app.widgets.card_viewers import PlainTextViewer

        _qapp()
        v = PlainTextViewer()
        v.set_width_cap(760)
        v.show()
        _qapp().processEvents()
        try:
            v.set_text("你好")
            assert v.height() < v.MAX_HEIGHT, f"短消息被误钉满高：{v.height()}"
        finally:
            v.cleanup()
            v.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
