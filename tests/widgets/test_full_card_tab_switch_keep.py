# -*- coding: utf-8 -*-
"""回归：full 类型浮动卡片切换标签页（切走再切回）不应被误关

Bug 链条（2026-08-27 排查）：
1. 标签页 A 打开 full 卡 X：_tab_card_visibility[A]={X}、_replace_open[A]={X}
2. 切到 B：TabManagerWindow._sync_overlay_cards_to_active_window 隐藏 X——
   _suppress_replace_close 保护了「关闭去抖」，但未保护 registry 的
   _on_hidden_for_tab 回调（该回调仅被 sync_floating_cards_to_tab 的
   _tab_sync_in_progress 标志豁免）→ X 从 _tab_card_visibility[A] 被误删
3. 切回 A：_sync_overlay_cards_to_active_window 按 open 恢复显示 X；
   随后 sync_floating_cards_to_tab(A) 因 want=False（集合被误删）
   & now=True 误执行 hide_card（无 _suppress_replace_close 保护）
   → 触发 120ms 关闭去抖 → _has_other_visible_full=False → 判定关闭：
   open 清空 + tab 移除 + hide_floating_card_globally → 卡片真正关闭

修复：_sync_overlay_cards_to_active_window 的投影 hide/show 也走
UIPluginRegistry.tab_sync_guard()（与 sync_floating_cards_to_tab 一致），
切换投影的临时隐藏不清除 per-tab 可见集合。
"""

from types import SimpleNamespace

import pytest
from PyQt5.QtWidgets import QApplication, QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID, CardManager
from app.widgets.tab_manager_window import TabManagerWindow


@pytest.fixture(autouse=True)
def reset_state():
    """重置 TabManagerWindow / UIPluginRegistry 单例（跨测试隔离）"""
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    UIPluginRegistry.get_instance().reset()
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    UIPluginRegistry.get_instance().reset()


class TestFullCardSurvivesTabSwitch:
    """full 卡在标签页间切走再切回：可见集合保留、不被误关"""

    @staticmethod
    def _make_tabs(qtbot, card_id: str):
        """构造 TabManagerWindow + 两个 fake 标签页（winA/winB），在 A 打开 full 卡"""
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        tm._windows = [
            SimpleNamespace(_window_id="winA"),
            SimpleNamespace(_window_id="winB"),
        ]
        tm._tab_panel._active_index = 0  # 当前活跃标签页 A
        reg = UIPluginRegistry.get_instance()
        # 模拟真实场景：此前已 sync 过标签页 A（_on_tab_selected 中 sync_floating_cards_to_tab
        # 先于下一次切换更新 _active_tab_scope——切换投影 hide 发生时它仍是旧标签页）
        reg._active_tab_scope = "winA"
        reg.register_floating_card("test-plugin", card_id, QWidget, "full", title="Full")
        reg.toggle_floating_card(card_id)
        for _ in range(3):
            QApplication.processEvents()
        return tm, reg

    @staticmethod
    def _switch_to(tm, index: int, scope: str):
        """模拟切换到指定标签页：覆盖层投影 + registry 投影（对齐 _on_tab_selected 顺序）"""
        tm._tab_panel._active_index = index
        tm._sync_overlay_cards_to_active_window()
        UIPluginRegistry.get_instance().sync_floating_cards_to_tab(scope)

    def test_switch_away_and_back_keeps_card(self, qtbot):
        """切到 B 再切回 A：卡片恢复显示，不被 120ms 去抖误判关闭"""
        card_id = "full-keep-card"
        tm, reg = self._make_tabs(qtbot, card_id)
        cm = CardManager.get_instance()

        # 打开后：A 集合有卡、卡片可见、A 的 open 有记录
        assert card_id in reg._tab_card_visibility.get("winA", set())
        assert cm.is_card_visible(card_id, GLOBAL_WINDOW_ID)
        assert card_id in tm._replace_open.get("winA", {})

        # ── 切到标签页 B ──
        self._switch_to(tm, 1, "winB")
        assert not cm.is_card_visible(card_id, GLOBAL_WINDOW_ID), "B 未打开该卡 → 隐藏"
        # 核心断言：临时隐藏不清除 A 的 per-tab 可见集合
        assert card_id in reg._tab_card_visibility.get("winA", set())
        assert card_id in tm._replace_open.get("winA", {})

        # ── 切回标签页 A ──
        self._switch_to(tm, 0, "winA")
        assert cm.is_card_visible(card_id, GLOBAL_WINDOW_ID), "切回后卡片恢复显示"
        assert card_id in reg._tab_card_visibility.get("winA", set())
        assert card_id in tm._replace_open.get("winA", {})

        # 120ms 关闭去抖窗口：不得出现关闭定时器 / 关闭动作
        assert not tm._replace_timers, "不应调度关闭去抖定时器"
        qtbot.wait(200)
        assert not tm._replace_timers
        assert card_id in tm._replace_open.get("winA", {}), "去抖窗口后卡片仍保留（未被误关）"

    def test_switch_away_twice_and_back_still_keeps_card(self, qtbot):
        """连续切走多次（A→B→A→B→A）：每次切回卡片都恢复显示"""
        card_id = "full-keep-twice"
        tm, reg = self._make_tabs(qtbot, card_id)
        cm = CardManager.get_instance()

        for i in range(2):
            self._switch_to(tm, 1, "winB")
            assert not cm.is_card_visible(card_id, GLOBAL_WINDOW_ID)
            assert card_id in reg._tab_card_visibility.get("winA", set())

            self._switch_to(tm, 0, "winA")
            assert cm.is_card_visible(card_id, GLOBAL_WINDOW_ID), f"第 {i + 1} 次切回后应恢复显示"
            assert card_id in reg._tab_card_visibility.get("winA", set())

        assert not tm._replace_timers

    def test_user_close_still_clears_collection(self, qtbot):
        """用户主动关闭（tab × / close_replace_card）仍清除集合——guard 不破坏关闭语义"""
        card_id = "full-close-card"
        tm, reg = self._make_tabs(qtbot, card_id)
        cm = CardManager.get_instance()
        assert card_id in reg._tab_card_visibility.get("winA", set())

        tm.close_replace_card(card_id)

        assert card_id not in reg._tab_card_visibility.get("winA", set()), "主动关闭应清除 per-tab 集合"
        assert card_id not in tm._replace_open.get("winA", {})
        assert not cm.is_card_visible(card_id, GLOBAL_WINDOW_ID)
