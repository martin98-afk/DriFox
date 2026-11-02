# -*- coding: utf-8 -*-
"""复现：UI 插件浮动卡（full/left）只能打开一次，第二次 toggle 无法再显示

用户路径：侧栏点击开卡 → 点卡片 X 关闭 → 再点侧栏 → 无反应。
[DEBUG-reopen] 临时复现用例，定位后转正式回归或删除。
"""

from types import SimpleNamespace

import pytest
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QApplication, QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID, CardManager
from app.widgets.tab_manager_window import TabManagerWindow


class _ClosedStub(QWidget):
    """带 closed 信号的最小桩卡（对齐 FloatingCardWidget 契约）"""

    closed = pyqtSignal()


def _load_real_cleaner_card():
    """以包形式加载真 SystemCleanerCard（包名对齐主程序加载器前缀）"""
    import importlib
    import sys
    import types

    pkg_name = "ui_plugin_system_cleaner"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [r"D:\work\DriFox\plugins\system-cleaner\ui"]
        sys.modules[pkg_name] = pkg
    cards = importlib.import_module(f"{pkg_name}.cards")
    return cards.SystemCleanerCard


@pytest.fixture(autouse=True)
def reset_state():
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    UIPluginRegistry.get_instance().reset()
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    UIPluginRegistry.get_instance().reset()


def _make_env(qtbot, container: str, card_id: str):
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm.show()
    _pump()
    tm._windows = [SimpleNamespace(_window_id="winA")]
    tm._tab_panel._active_index = 0
    reg = UIPluginRegistry.get_instance()
    reg._active_tab_scope = "winA"
    reg.register_floating_card("test-plugin", card_id, _ClosedStub, container, title="Repro")
    return tm, reg


def _pump(n: int = 3):
    for _ in range(n):
        QApplication.processEvents()


def _close_like_user_x(widget):
    """模拟用户点卡片 X：_on_close 契约 = 隐藏 + closed 信号"""
    widget.setVisible(False)
    widget.closed.emit()


@pytest.mark.parametrize("container", ["full", "left"])
def test_close_then_reopen_floating_card(qtbot, container):
    """开 → 关（模拟点 X）→ 再开：第二次必须能重新显示"""
    card_id = f"reopen-{container}"
    tm, reg = _make_env(qtbot, container, card_id)
    cm = CardManager.get_instance()

    # 第一次打开
    reg.toggle_floating_card(card_id)
    _pump()
    widget = reg.get_floating_card_widget(card_id)
    assert widget is not None
    assert cm.is_card_visible(card_id, GLOBAL_WINDOW_ID), f"[{container}] 第一次打开失败"
    assert widget.isVisible(), f"[{container}] 第一次打开 widget 不可见"

    # 模拟用户点 X：widget 隐藏 + closed 信号 → registry lambda → hide_card
    _close_like_user_x(widget)
    _pump()
    assert not cm.is_card_visible(card_id, GLOBAL_WINDOW_ID), f"[{container}] 关闭后记账未清"

    # 第二次打开（复现点）
    reg.toggle_floating_card(card_id)
    _pump()
    assert cm.is_card_visible(card_id, GLOBAL_WINDOW_ID), f"[{container}] 第二次记账不可见"
    assert widget.isVisible(), f"[{container}] 第二次打开 widget 不可见（用户视角：打不开）"


@pytest.mark.parametrize("container", ["full", "left"])
def test_real_cleaner_card_close_reopen(qtbot, container):
    """真 SystemCleanerCard：开 → 点 X 关 → 再开"""
    SystemCleanerCard = _load_real_cleaner_card()
    card_id = f"real-cleaner-{container}"
    tm, reg = _make_env(qtbot, container, card_id)
    cm = CardManager.get_instance()

    reg.toggle_floating_card(card_id)
    _pump()
    widget = reg.get_floating_card_widget(card_id)
    assert widget is not None
    assert widget.isVisible(), f"[{container}] 真卡第一次打开不可见"

    _close_like_user_x(widget)
    _pump()
    assert not cm.is_card_visible(card_id, GLOBAL_WINDOW_ID)

    reg.toggle_floating_card(card_id)
    _pump()
    assert widget.isVisible(), f"[{container}] 真卡第二次打开不可见（用户视角：打不开）"


@pytest.mark.parametrize("container", ["full", "left"])
def test_move_then_close_then_reopen(qtbot, container):
    """右键改位置（move）→ 开 → 关 → 再开：move 销毁重建后的生命周期"""
    card_id = f"move-reopen-{container}"
    tm, reg = _make_env(qtbot, container, card_id)
    cm = CardManager.get_instance()

    # 先开一次（top 初始方位），再 move 到目标方位（触发销毁重建）
    reg.toggle_floating_card(card_id)
    _pump()
    assert reg.move_floating_card(card_id, container) is True
    _pump()
    widget = reg.get_floating_card_widget(card_id)
    assert widget is not None, f"[{container}] move 后实例未重建"
    assert widget.isVisible(), f"[{container}] move 后卡片不可见"

    # 点 X 关闭
    _close_like_user_x(widget)
    _pump()
    assert not cm.is_card_visible(card_id, GLOBAL_WINDOW_ID)

    # 再开（复现点）
    reg.toggle_floating_card(card_id)
    _pump()
    widget2 = reg.get_floating_card_widget(card_id)
    assert widget2 is not None
    assert widget2.isVisible(), f"[{container}] move 后关闭再开不可见（用户视角：打不开）"


@pytest.mark.parametrize("container", ["full", "left"])
def test_plugin_reload_then_reopen(qtbot, container):
    """插件热重载（unload+register）→ 再开：旧实例销毁后记账是否残留"""
    card_id = f"reload-reopen-{container}"
    tm, reg = _make_env(qtbot, container, card_id)
    cm = CardManager.get_instance()

    reg.toggle_floating_card(card_id)
    _pump()
    assert reg.get_floating_card_widget(card_id).isVisible()

    # 模拟热重载：unload 清实例缓存（对齐 unload_plugin 的清理面），再重新注册
    reg.unload_plugin("test-plugin")
    _pump()
    reg.register_floating_card("test-plugin", card_id, _ClosedStub, container, title="Repro")
    _pump()

    # 重载后第一次点侧栏（对应用户视角的「第二次打开」）
    reg.toggle_floating_card(card_id)
    _pump()
    widget = reg.get_floating_card_widget(card_id)
    assert widget is not None, f"[{container}] 重载后实例未创建"
    assert widget.isVisible(), f"[{container}] 重载后打开不可见（用户视角：打不开）"


@pytest.mark.parametrize("container", ["full", "left"])
def test_reopen_with_sibling_card_mutex(qtbot, container):
    """同容器有其他卡共存（互斥）：开 A → 开 B（A 被互斥藏）→ 关 B → 再开 A"""
    card_a = f"mutex-a-{container}"
    card_b = f"mutex-b-{container}"
    tm, reg = _make_env(qtbot, container, card_a)
    cm = CardManager.get_instance()
    reg.register_floating_card("test-plugin", card_b, _ClosedStub, container, title="ReproB")

    reg.toggle_floating_card(card_a)
    _pump()
    wa = reg.get_floating_card_widget(card_a)
    assert wa.isVisible(), f"[{container}] A 首开不可见"

    # 开 B（同容器互斥应藏 A）
    reg.toggle_floating_card(card_b)
    _pump()
    wb = reg.get_floating_card_widget(card_b)
    assert wb.isVisible(), f"[{container}] B 开不可见"

    # 关 B
    _close_like_user_x(wb)
    _pump()

    # 再开 A（复现点）
    reg.toggle_floating_card(card_a)
    _pump()
    assert wa.isVisible(), f"[{container}] 互斥交互后 A 再开不可见（用户视角：打不开）"


def test_full_card_tab_switch_then_reopen(qtbot):
    """full 卡：开 → 切标签页 B 再切回 → 关 → 再开"""
    card_id = "tabswitch-full"
    tm, reg = _make_env(qtbot, "full", card_id)
    cm = CardManager.get_instance()

    reg.toggle_floating_card(card_id)
    _pump()
    widget = reg.get_floating_card_widget(card_id)
    assert widget.isVisible()

    # 切到 B 再切回（对齐 _on_tab_selected 顺序）
    tm._tab_panel._active_index = 1
    tm._sync_overlay_cards_to_active_window()
    reg.sync_floating_cards_to_tab("winB")
    _pump()
    tm._tab_panel._active_index = 0
    tm._sync_overlay_cards_to_active_window()
    reg.sync_floating_cards_to_tab("winA")
    _pump()
    assert widget.isVisible(), "切回后应恢复显示"

    # 关 → 再开（复现点）
    _close_like_user_x(widget)
    _pump()
    reg.toggle_floating_card(card_id)
    _pump()
    assert widget.isVisible(), "tab 切换后关闭再开不可见（用户视角：打不开）"

