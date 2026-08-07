# -*- coding: utf-8 -*-
"""TabItem 关闭按钮内联二次确认测试（防误触误删）

背景：标签页关闭按钮此前点击一次即触发 tabCloseRequested，容易误删进行中的对话。
参照 worktree_section 删除按钮交互，但仅在**存在进行中状态**时启用二次确认：

- 流式对话（_streaming）或提问等待（_question）→ 首次点击进入确认态
  （按钮变红"确认关闭"），二次点击才真正关闭；移出 / 3 秒超时自动取消。
- 对话已结束（无状态）→ 直接关闭，不打扰。

覆盖范围：
① 活跃tab（流式）：首次点击不发射 closeRequested，进入确认态（文字/红色样式）
② 活跃tab（流式）：二次点击才真正发射 closeRequested
③ 活跃tab（提问等待）：同样需要二次确认
④ 非活跃tab（对话结束）：点击一次直接关闭，不进入确认态
⑤ 移出 Tab（leaveEvent）取消确认态，恢复普通样式
⑥ 选中态切换（set_active_index）取消旧 tab 确认态
⑦ 超时自动取消（timer 触发）
"""

from unittest.mock import MagicMock

import pytest


def _make_item(panel=None):
    """构造真实 TabItem（仅面板 mock，控件全真实）"""
    from app.widgets.tab_panel import TabItem

    item = TabItem("测试 Tab", None, None, panel=panel)
    return item


def _make_panel():
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    # 隔离外部依赖（菜单等）
    panel._on_right_click = MagicMock()
    return panel


# ══════════════════════════════════════════════════════════
# ① 活跃tab（流式）：首次点击进入确认态
# ══════════════════════════════════════════════════════════


def test_streaming_first_click_enters_confirm_state(qapp):
    """流式对话中：首次点击不发射 closeRequested，按钮变为「确认关闭」"""
    item = _make_item(None)
    item.set_streaming(True)
    try:
        emitted = []
        item.closeRequested.connect(lambda: emitted.append(1))

        item._on_close_btn_clicked()

        assert emitted == [], "首次点击不得真正关闭"
        assert item._confirming_close is True
        assert item._close_btn.text() == "确认关闭"
        assert item._close_btn.isEnabled()
        assert item._close_timer.isActive(), "确认态应启动 3 秒自动取消定时器"
    finally:
        item.deleteLater()


# ══════════════════════════════════════════════════════════
# ② 活跃tab：二次点击真正关闭
# ══════════════════════════════════════════════════════════


def test_streaming_second_click_confirms_close(qapp):
    """流式对话确认态内二次点击：才真正发射 closeRequested 并禁用按钮防重入"""
    item = _make_item(None)
    item.set_streaming(True)
    try:
        emitted = []
        item.closeRequested.connect(lambda: emitted.append(1))

        item._on_close_btn_clicked()  # 首次：进确认态
        item._on_close_btn_clicked()  # 二次：确认关闭

        assert emitted == [1]
        assert item._confirming_close is False
        assert item._close_btn.isEnabled() is False, "确认后禁用按钮防重入"
    finally:
        item.deleteLater()


# ══════════════════════════════════════════════════════════
# ③ 活跃tab（提问等待）：同样需要二次确认
# ══════════════════════════════════════════════════════════


def test_question_first_click_enters_confirm_state(qapp):
    """提问等待回答中：首次点击进入确认态（不直接关闭）"""
    item = _make_item(None)
    item.set_question(True)
    try:
        emitted = []
        item.closeRequested.connect(lambda: emitted.append(1))

        item._on_close_btn_clicked()

        assert emitted == []
        assert item._confirming_close is True
    finally:
        item.deleteLater()


# ══════════════════════════════════════════════════════════
# ④ 非活跃tab（对话结束）：直接关闭不确认
# ══════════════════════════════════════════════════════════


def test_idle_first_click_closes_directly(qapp):
    """对话已结束（非流式非提问）：点击一次直接关闭，不进入确认态"""
    item = _make_item(None)
    try:
        emitted = []
        item.closeRequested.connect(lambda: emitted.append(1))

        item._on_close_btn_clicked()

        assert emitted == [1], "对话结束应直接关闭"
        assert item._confirming_close is False, "不应进入确认态"
        assert item._close_timer.isActive() is False
    finally:
        item.deleteLater()


def test_streaming_stop_after_confirm_makes_second_click_close(qapp):
    """确认态期间流式结束：二次点击仍按确认关闭处理（用户已明确点了确认）"""
    item = _make_item(None)
    item.set_streaming(True)
    try:
        emitted = []
        item.closeRequested.connect(lambda: emitted.append(1))

        item._on_close_btn_clicked()  # 进入确认态
        assert item._confirming_close is True

        # 流式结束
        item.set_streaming(False)

        # 用户看到确认态按钮，第二次点击 → 关闭
        item._on_close_btn_clicked()
        assert emitted == [1]
    finally:
        item.deleteLater()


# ══════════════════════════════════════════════════════════
# ⑤ 移出取消
# ══════════════════════════════════════════════════════════


def test_leave_event_cancels_confirm(qapp):
    """确认态中 leaveEvent：取消确认，恢复普通关闭按钮样式"""
    item = _make_item(None)
    item.set_streaming(True)
    try:
        item._on_close_btn_clicked()
        assert item._confirming_close is True

        item.leaveEvent(None)

        assert item._confirming_close is False
        assert item._close_timer.isActive() is False
        assert item._close_btn.text() == ""
        assert item._close_btn.isEnabled()
    finally:
        item.deleteLater()


# ══════════════════════════════════════════════════════════
# ⑥ 切换选中取消旧 tab 确认态
# ══════════════════════════════════════════════════════════


def test_set_active_index_cancels_old_confirm(qapp):
    """切换选中 Tab：旧 tab 的关闭确认态必须被取消"""
    panel = _make_panel()
    try:
        idx0 = panel.add_tab("A")
        panel.add_tab("B")
        panel.set_active_index(idx0)
        panel._items[idx0].set_streaming(True)

        # 进入确认态后切换到 B
        panel._items[idx0]._on_close_btn_clicked()
        assert panel._items[idx0]._confirming_close is True

        panel.set_active_index(1)

        assert panel._items[idx0]._confirming_close is False, "切换选中应取消旧 tab 确认态"
    finally:
        panel.deleteLater()


# ══════════════════════════════════════════════════════════
# ⑦ 超时自动取消
# ══════════════════════════════════════════════════════════


def test_timer_timeout_cancels_confirm(qapp):
    """3 秒超时自动取消确认态（timer 触发 _cancel_close_confirm）"""
    item = _make_item(None)
    item.set_streaming(True)
    try:
        item._on_close_btn_clicked()
        assert item._confirming_close is True

        # 手动触发超时（等价 3 秒后）
        item._cancel_close_confirm()

        assert item._confirming_close is False
        assert item._close_timer.isActive() is False
        assert item._close_btn.text() == ""
    finally:
        item.deleteLater()
