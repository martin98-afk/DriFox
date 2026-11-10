# -*- coding: utf-8 -*-
"""[A3-C] 方案 C：非活动标签卡片延迟销毁（DRIFOX_TAB_KEEP_SECS）。

覆盖（#26 规格 1-5）：
1. 默认 0 → 切走即走原销毁链（keep=False，行为与改前一致）
2. >0 → 切走后卡片存活未 deleteLater；切回零重建（同一 widget 对象回布局）
3. 到期 → 原销毁链执行（cleanup/deleteLater）、槽清空、定时器停
4. A→B→A→B → 只保留最近一个，不累积
5. 非法值/负值 → 回退 0

__new__ 最小实例（与 test_virtual_scroll_recycle_rebuild_loop 同法，不触 WebEngine）。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QLayout, QVBoxLayout, QWidget

from app.main_widget import OpenAIChatToolWindow


def _is_deleted(w) -> bool:
    import sip

    return sip.isdeleted(w)


class _Host(QWidget):
    """chat_layout 宿主（offscreen 不显示）"""


@pytest.fixture
def win(qapp):
    w = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    for key, value in {
        "_is_destroyed": False,
        "_displayed_session_id": None,
        "_current_session_id": None,
        "_kept_session_tab": None,
        "_batch_cards": [],
        "_message_batch": [],
        "_user_prefix_cache": [0],
        "_node_batch_map_cache": None,
        "_last_visible_card_ids": set(),
        "_visible_batch_start": 0,
        "_visible_batch_end": 0,
        "_is_loading_history_batches": False,
        "_lazy_batch_timer_active": False,
        "_pending_lazy_cards": [],
        "_current_assistant_card": None,
    }.items():
        setattr(w, key, value)
    # 生产语义的活性判定（sip.isdeleted）
    w._is_widget_alive = lambda x: not _is_deleted(x)
    w._clear_pending_diff_stats = MagicMock()
    w._reset_bottom_follow = MagicMock()
    w._sync_global_rendered_pages = MagicMock()
    host = _Host()
    w._probe_host = host  # 保持引用：局部变量被 GC 会连带删掉布局的 C++ 对象
    lay = QVBoxLayout(host)
    # chat_layout 需要的布局语义：count/takeAt/addWidget/removeWidget
    w.chat_layout = lay
    return w


def _make_cards(w, n, prefix="c"):
    """造 n 张假卡片塞进 chat_layout，并同步 _batch_cards/_message_batch"""
    cards = []
    for i in range(n):
        c = QWidget()
        c.cleanup = MagicMock()
        c.setObjectName(f"{prefix}_{i}")
        cards.append(c)
        w.chat_layout.addWidget(c)
    w._batch_cards = [[c] for c in cards]
    w._message_batch = [[{"role": "user"}] for _ in cards]
    w._user_prefix_cache = [0]
    return cards


def test_default_zero_keeps_old_behavior(monkeypatch, win):
    """1. 默认 0：_keep_previous_session_tab 返回 False（走原销毁链），槽不建"""
    monkeypatch.delenv("DRIFOX_TAB_KEEP_SECS", raising=False)
    assert win._load_tab_keep_secs() == 0
    _make_cards(win, 2)
    win._displayed_session_id = "s1"
    assert win._keep_previous_session_tab() is False
    assert win._kept_session_tab is None


def test_keep_then_restore_same_widgets(monkeypatch, win):
    """2. >0：切走保留（卡片存活未 deleteLater）；切回同一对象回布局零重建"""
    monkeypatch.setenv("DRIFOX_TAB_KEEP_SECS", "30")
    cards = _make_cards(win, 2)
    snap_ref = win._batch_cards  # 快照应还原到同一列表对象
    win._displayed_session_id = "s1"
    assert win._keep_previous_session_tab() is True
    kept = win._kept_session_tab
    assert kept is not None and kept["sid"] == "s1"
    for c in cards:
        assert not _is_deleted(c), "保留期内卡片被销毁"
    assert kept["timer"].isActive()

    # 新会话重建覆盖状态（模拟切换后的窗口状态）
    win._batch_cards = [None]
    win._message_batch = [[]]

    # 切回：同一 widget 对象恢复（零重建）
    assert win._restore_kept_session("s1") is True
    assert win._kept_session_tab is None
    assert win._batch_cards is snap_ref  # 快照还原（同一列表对象）
    assert win._batch_cards[0][0] is cards[0]
    for c in cards:
        assert not _is_deleted(c)


def test_expire_runs_destroy_chain(monkeypatch, win):
    """3. 到期：原销毁链（cleanup + 脱父 + deleteLater）执行、槽清空、定时器停"""
    monkeypatch.setenv("DRIFOX_TAB_KEEP_SECS", "30")
    cards = _make_cards(win, 2)
    win._displayed_session_id = "s1"
    win._keep_previous_session_tab()
    kept = win._kept_session_tab
    for c in cards:
        c.cleanup = MagicMock()
        c.setParent(win.chat_layout.parentWidget())
    win._expire_kept_session()
    for c in cards:
        c.cleanup.assert_called_once()
        assert c.parent() is None, "销毁链未解除父引用"
    assert win._kept_session_tab is None
    assert not kept["timer"].isActive()


def test_single_slot_no_accumulation(monkeypatch, win):
    """4. A→B→A→B 快速切换：只保留最近一个，旧保留先走销毁链"""
    monkeypatch.setenv("DRIFOX_TAB_KEEP_SECS", "30")
    cards_a = _make_cards(win, 1, prefix="A")
    win._displayed_session_id = "A"
    win._keep_previous_session_tab()
    kept_b = _make_cards(win, 1, prefix="B")
    win._displayed_session_id = "B"
    win._keep_previous_session_tab()  # 名额被占：A 保留先销毁
    assert win._kept_session_tab["sid"] == "B"
    from PyQt5.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)  # 强投删除队列
    assert _is_deleted(cards_a[0]), "被挤出的 A 卡未走销毁链"
    _make_cards(win, 1, prefix="A2")
    win._displayed_session_id = "A"
    win._keep_previous_session_tab()
    assert win._kept_session_tab["sid"] == "A"
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert _is_deleted(kept_b[0]), "被挤出的 B 卡未走销毁链"


def test_kept_pid_protected_by_guard(win, monkeypatch):
    """第 2 步-5：保留期内卡片 pid 必须让 _pid_still_in_use 返回 True（防 T22/T31 误杀）"""
    monkeypatch.setenv("DRIFOX_TAB_KEEP_SECS", "30")
    cards = _make_cards(win, 2)
    for i, c in enumerate(cards):
        c._renderer_pid = 1000 + i
    win._displayed_session_id = "s1"
    win._keep_previous_session_tab()
    # 保留期：pid 受保护
    assert win._pid_still_in_use(1000) is True
    assert win._pid_still_in_use(1001) is True
    assert win._pid_still_in_use(9999) is False
    # 到期销毁后：保护解除（生产时序：到期前新会话已重建 _batch_cards；
    # 强投 DeferredDelete 让 sip 视作已删）
    win._batch_cards = [None]
    win._current_assistant_card = None
    win._expire_kept_session()
    from PyQt5.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert win._pid_still_in_use(1000) is False


def test_invalid_env_falls_back_to_zero(win, monkeypatch):
    """5. 非法值/负值/0 → 回退 0"""
    for bad in ("", "abc", "-1", "0"):
        monkeypatch.setenv("DRIFOX_TAB_KEEP_SECS", bad)
        assert win._load_tab_keep_secs() == 0, f"非法值 {bad!r} 未回退 0"
    monkeypatch.setenv("DRIFOX_TAB_KEEP_SECS", "5")
    assert win._load_tab_keep_secs() == 5
