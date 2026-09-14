# -*- coding: utf-8 -*-
"""回归：流式结束终渲染期间「程序滚动」不得被误判成用户上滚

Bug 复现（2026-09-15 实机日志，标签 [scroll-diag]）：
```
card-delta        value=584 max=862  gap=278 delta=151   # 宿主按单卡 delta 补偿 → 落点 735
away-set          value=735 max=862  gap=127 away=True   # 补偿落点 < 真实 maximum → 误判
card-loaded-skip  value=735 max=1198 gap=463 away=True   # 跟底守卫全关，视口冻结
card-loaded-skip  value=735 max=1014 gap=279 away=True   # 最终离底 279px，只能手动滚
```

断链三步：
1. 终渲染几百毫秒内高度连涨多拍，按「单卡 delta」累加的补偿落点系统性偏小；
2. 这次落后的 setValue 触发 valueChanged，`_on_scroll_changed` 按「不在底部」
   把 `_user_intentionally_away_from_bottom` 置 True；
3. away 置位后所有跟底守卫关闭 → value 不再变化 → 没有新的 valueChanged →
   away 永不复位（死锁），视口停在消息列表中段。

修复：程序发起的 setValue 一律包在 `_programmatic_scroll()` 上下文里，
`_on_scroll_changed` 据此豁免 away 置位，锚定期也不因自己的欠补偿取消锚定；
流式结束给 1500ms sticky 锚定，每 100ms 强制 setValue(校正后的 maximum)。
"""

import time
from unittest.mock import MagicMock

import pytest

# 注意：不得在此插入 "app" 到 sys.path——app/plugins（regular package）会
# 劫持顶层 `plugins.*` 命名空间解析。仓库根已由 pytest rootdir 提供。

from app.main_widget import OpenAIChatToolWindow  # noqa: E402


def _make_win(**overrides):
    """构造 `_on_scroll_changed` / `_maintain_bottom_anchor` 可运行的最小实例。

    与既有 test_main_widget_scroll_anchor.py 同策略：不实例化完整窗口
    （依赖过多），`__new__` 后手工补齐属性。
    """
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    # ⚠️ PyQt 对象未经 __init__ 时 getattr(self, "_is_destroyed", False) 会抛
    # RuntimeError，必须显式给值。
    win._is_destroyed = False
    win._loading_session = False
    win._user_intentionally_away_from_bottom = False
    win._programmatic_scroll_depth = 0
    win._bottom_anchor_deadline = 0.0
    win._bottom_anchor_timer = MagicMock()
    win._sync_node_preview_to_scroll = MagicMock()
    win._virtual_scroll_timer = MagicMock()
    win._scroll_sync_timer = MagicMock()
    win._load_more_history_batches = MagicMock()
    win._history_load_threshold = 48
    # 真实 `_sync_scroll_maximum` 会拿 container.sizeHint() 做算术，MagicMock
    # 参与比较会抛 TypeError —— 这里直接桩掉，`_is_view_at_bottom` 只看
    # scroll_bar 的 maximum/value。
    win._sync_scroll_maximum = MagicMock(return_value=862)

    scroll_bar = MagicMock()
    scroll_bar.maximum.return_value = 862
    scroll_bar.value.return_value = 735  # 离底 127px：补偿欠账的典型现场
    area = MagicMock()
    area.verticalScrollBar.return_value = scroll_bar
    win.chat_scroll_area = area
    for key, value in overrides.items():
        setattr(win, key, value)
    return win


@pytest.fixture(autouse=True)
def _silence_diag(monkeypatch):
    """诊断打点默认开启，测试里关掉，避免刷日志"""
    import app.main_widget as mw

    monkeypatch.setattr(mw, "_SCROLL_DIAG_ENABLED", False)


# ─── 1. away 置位必须排除程序滚动 ────────────────────────────────


def test_programmatic_scroll_never_sets_away():
    """程序 setValue 落点落后于 maximum 时，不得记成「用户滚离底部」"""
    win = _make_win()
    with win._programmatic_scroll():
        win._on_scroll_changed(735)
    assert win._user_intentionally_away_from_bottom is False


def test_user_scroll_still_sets_away():
    """守护：真实用户滚离底部仍然置 away（修复不得把守卫一起废掉）"""
    win = _make_win()
    win._on_scroll_changed(735)
    assert win._user_intentionally_away_from_bottom is True


def test_away_reset_still_works_inside_programmatic_scroll():
    """程序滚到底部时，away 仍要按既有语义复位"""
    win = _make_win(_user_intentionally_away_from_bottom=True)
    win.chat_scroll_area.verticalScrollBar().value.return_value = 862  # 贴底
    with win._programmatic_scroll():
        win._on_scroll_changed(862)
    assert win._user_intentionally_away_from_bottom is False


# ─── 2. 锚定期不得被自己的欠补偿取消 ─────────────────────────────


def test_programmatic_scroll_does_not_cancel_bottom_anchor():
    """终渲染期间补偿落点 < maximum 时，不能顺手把 sticky 锚定踹掉"""
    win = _make_win()
    win._bottom_anchor_deadline = time.monotonic() + 1.0
    with win._programmatic_scroll():
        win._on_scroll_changed(735)
    assert win._bottom_anchor_deadline > time.monotonic()
    win._bottom_anchor_timer.stop.assert_not_called()


def test_user_scroll_cancels_bottom_anchor():
    """守护：真实用户滚动立即取消锚定（否则 sticky 会反过来打断阅读）"""
    win = _make_win()
    win._bottom_anchor_deadline = time.monotonic() + 1.0
    win._on_scroll_changed(735)
    assert win._bottom_anchor_deadline == 0.0
    win._bottom_anchor_timer.stop.assert_called_once()


# ─── 3. 锚定维护要用校正后的上界 ─────────────────────────────────


def test_maintain_bottom_anchor_uses_synced_maximum():
    """锚定维护必须走 _sync_scroll_maximum，否则一直拿滞后的旧上界置底"""
    win = _make_win(_bottom_anchor_deadline=time.monotonic() + 1.0)
    sb = win.chat_scroll_area.verticalScrollBar()
    sb.maximum.return_value = 800  # Qt 现值（布局尚未传播，滞后）
    win._sync_scroll_maximum = MagicMock(return_value=1234)  # sizeHint 校正值

    win._maintain_bottom_anchor()

    sb.setValue.assert_called_once_with(1234)
    win._bottom_anchor_timer.start.assert_called_once()
