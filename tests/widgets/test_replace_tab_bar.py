# -*- coding: utf-8 -*-
"""替换类型(full 容器)卡片顶部 tab 栏测试

覆盖：
- ReplaceTabBar 控件单元的增删/高亮/重建/信号
- TabManagerWindow 对 full 卡片显隐事件的同步（≥1 即显示、单卡片也显示、
  非 full 卡片忽略、互斥切换保留、用户关闭后剩余自动激活）
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.widgets.replace_tab_bar import ReplaceTabBar
from app.widgets.tab_manager_window import TabManagerWindow


@pytest.fixture(autouse=True)
def _isolate_tab_manager():
    TabManagerWindow._instance = None
    yield
    inst = TabManagerWindow._instance
    if inst is not None:
        for t in list(getattr(inst, "_replace_timers", {}).values()):
            try:
                t.stop()
            except Exception:
                pass
        TabManagerWindow._instance = None


def test_replace_tab_bar_add_remove(qtbot):
    bar = ReplaceTabBar()
    qtbot.addWidget(bar)
    bar.add_tab("a", "用量统计")
    bar.add_tab("b", "插件市场")
    assert list(bar._buttons) == ["a", "b"]
    bar.set_active("b")
    assert bar._buttons["b"]._active and not bar._buttons["a"]._active
    bar.remove_tab("a")
    assert list(bar._buttons) == ["b"]


def test_replace_tab_bar_set_tabs(qtbot):
    bar = ReplaceTabBar()
    qtbot.addWidget(bar)
    bar.set_tabs({"a": "A", "c": "C"}, "c")
    assert set(bar._buttons) == {"a", "c"}
    assert bar._buttons["c"]._active
    # 移除 c，保留 a
    bar.set_tabs({"a": "A"}, "a")
    assert list(bar._buttons) == ["a"]


def test_replace_tab_bar_signals(qtbot):
    bar = ReplaceTabBar()
    qtbot.addWidget(bar)
    bar.add_tab("a", "A")
    clicks, closes = [], []
    bar.tabClicked.connect(clicks.append)
    bar.tabCloseClicked.connect(closes.append)
    bar._buttons["a"].clicked.emit("a")
    bar._buttons["a"].closeClicked.emit("a")
    assert clicks == ["a"] and closes == ["a"]


def test_full_cards_tab_bar_sync(qtbot, monkeypatch):
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)

    # spy on setVisible 以验证显隐意图，避免依赖真实窗口可见性
    spied = []
    monkeypatch.setattr(tm._replace_tab_bar, "setVisible", lambda v: spied.append(v))

    reg = MagicMock()
    reg.get_floating_cards.return_value = {
        "usage": SimpleNamespace(container="full", title="用量统计"),
        "market": SimpleNamespace(container="full", title="插件市场"),
        "settings": SimpleNamespace(container="top", title="设置"),
    }
    monkeypatch.setattr(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance",
        lambda: reg,
    )
    visible = set()
    cm = MagicMock()

    def fake_is_visible(cid, wid):
        return cid in visible

    cm.is_card_visible.side_effect = fake_is_visible
    monkeypatch.setattr(
        "app.widgets.cards.card_manager.CardManager.get_instance",
        lambda: cm,
    )

    # 非 full 卡片事件被忽略
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert "settings" not in tm._replace_open

    # 开第一个 full → 单卡片也显示（≥1 即显示）
    visible.add("usage")
    tm._on_card_visibility_changed({"card_id": "usage", "visible": True})
    assert "usage" in tm._replace_open
    assert spied[-1] is True

    # 开第二个 → 仍显示（互斥：usage 被隐藏，market 显示）
    visible.discard("usage")
    visible.add("market")
    tm._on_card_visibility_changed({"card_id": "market", "visible": True})
    assert set(tm._replace_open) == {"usage", "market"}
    assert spied[-1] is True

    # 互斥切换：隐藏 usage（market 仍可见）→ 保留 usage
    tm._on_card_visibility_changed({"card_id": "usage", "visible": False})
    qtbot.wait(200)
    assert "usage" in tm._replace_open

    # 关闭 market（最后可见的）→ 移除；剩 usage（1 个）→ 仍显示（自动激活剩余）
    visible.discard("market")
    tm._on_card_visibility_changed({"card_id": "market", "visible": False})
    qtbot.wait(200)
    assert "market" not in tm._replace_open
    assert spied[-1] is True


def test_replace_tab_bar_has_conversation_button(qtbot):
    from app.widgets.replace_tab_bar import CONVERSATION_ID

    bar = ReplaceTabBar()
    qtbot.addWidget(bar)
    # 常驻「对话」按钮始终存在（位于所有 replace 按钮最左）
    assert bar._conv_btn._card_id == CONVERSATION_ID
    assert bar._conv_btn._is_conversation is True
    # 对话按钮无关闭按钮（无法「关闭对话」）
    assert not hasattr(bar._conv_btn, "_close")
    # 对话按钮固定为 btn_layout 首个子项
    assert bar._btn_layout.itemAt(0).widget() is bar._conv_btn


def test_global_replace_cards_sync(qtbot, monkeypatch):
    from app.widgets.replace_tab_bar import CONVERSATION_ID

    TabManagerWindow._instance = None
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active = None
    tm._replace_timers.clear()

    spied = []
    monkeypatch.setattr(tm._replace_tab_bar, "setVisible", lambda v: spied.append(v))

    reg = MagicMock()
    reg.get_floating_cards.return_value = {}  # 无浮动卡
    monkeypatch.setattr(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance",
        lambda: reg,
    )
    visible = set()
    cm = MagicMock()

    def fake_is_visible(cid, wid):
        return cid in visible

    cm.is_card_visible.side_effect = fake_is_visible
    monkeypatch.setattr(
        "app.widgets.cards.card_manager.CardManager.get_instance",
        lambda: cm,
    )

    # 非 GLOBAL_WINDOW_ID 作用域的卡片事件被忽略
    tm._on_card_visibility_changed({"card_id": "settings", "window_id": "other", "visible": True})
    _snap = dict(tm._replace_open)
    assert "settings" not in _snap

    # 内置全局卡「系统设置」显示 → 进 open 且 tab 显示（对话按钮常驻）
    visible.add("settings")
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert tm._replace_open.get("settings") == "系统设置"
    assert spied[-1] is True
    assert tm._replace_tab_bar._conv_btn._card_id == CONVERSATION_ID

    # 点「对话」→ 隐藏所有 replace 卡片回到对话区，但保留 open（tab 栏常驻对话项、对话高亮）
    tm._on_replace_tab_clicked(CONVERSATION_ID)
    assert "settings" in tm._replace_open
    assert tm._replace_active == CONVERSATION_ID
    assert spied[-1] is True

    # 子智能体会话显示 → 进 open（标题取内置映射）
    visible.add("sub_agent_session")
    tm._on_card_visibility_changed({"card_id": "sub_agent_session", "visible": True})
    assert tm._replace_open.get("sub_agent_session") == "子智能体会话"
    assert spied[-1] is True


def test_replace_tab_disappears_on_card_close(qtbot, monkeypatch):
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active = None
    tm._replace_timers.clear()
    spied = []
    monkeypatch.setattr(tm._replace_tab_bar, "setVisible", lambda v: spied.append(v))
    reg = MagicMock()
    reg.get_floating_cards.return_value = {}
    monkeypatch.setattr(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance",
        lambda: reg,
    )
    visible = set()
    cm = MagicMock()
    cm.is_card_visible.side_effect = lambda cid, wid: cid in visible
    monkeypatch.setattr(
        "app.widgets.cards.card_manager.CardManager.get_instance",
        lambda: cm,
    )

    # 系统设置显示 → 进 open
    visible.add("settings")
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert "settings" in tm._replace_open

    # 关闭系统设置（hidden 事件）→ 移除 tab、tab 栏随之隐藏
    visible.discard("settings")
    tm._on_card_visibility_changed({"card_id": "settings", "visible": False})
    # 直接触发关闭去抖超时（验证移除逻辑，绕过 QTimer 跨测试时序污染）
    tm._on_replace_close_timeout("settings")
    assert "settings" not in tm._replace_open
    assert spied[-1] is False


def test_conversation_button_keeps_open(qtbot, monkeypatch):
    """点「对话」仅切换视图，不销毁 open 集合（tab 栏常驻对话项）"""
    from app.widgets.replace_tab_bar import CONVERSATION_ID

    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active = None
    tm._replace_timers.clear()
    spied = []
    monkeypatch.setattr(tm._replace_tab_bar, "setVisible", lambda v: spied.append(v))
    reg = MagicMock()
    reg.get_floating_cards.return_value = {}
    monkeypatch.setattr(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance",
        lambda: reg,
    )
    visible = set()
    cm = MagicMock()
    cm.is_card_visible.side_effect = lambda cid, wid: cid in visible
    monkeypatch.setattr(
        "app.widgets.cards.card_manager.CardManager.get_instance",
        lambda: cm,
    )

    # 系统设置显示
    visible.add("settings")
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert "settings" in tm._replace_open
    assert spied[-1] is True

    # 点「对话」→ 隐藏 settings 但保留 open，tab 栏仍显示且对话高亮
    tm._on_replace_tab_clicked(CONVERSATION_ID)
    assert "settings" in tm._replace_open
    assert tm._replace_active == CONVERSATION_ID
    assert spied[-1] is True
    assert tm._replace_tab_bar._conv_btn._active is True

    # 再点系统设置 → 回到该卡，对话取消高亮
    tm._on_replace_tab_clicked("settings")
    assert tm._replace_active == "settings"
    assert tm._replace_tab_bar._conv_btn._active is False


def test_replace_close_via_qtimer(qtbot, monkeypatch):
    """真实 QTimer 去抖链路：hidden 事件 → 120ms → 移除 tab 且 tab 栏隐藏

    验证运行时设置卡片关闭后 tab 消失（不依赖直接调 timeout 回调绕过时序）。
    """
    from app.widgets.replace_tab_bar import CONVERSATION_ID

    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active = None
    tm._replace_timers.clear()
    spied = []
    monkeypatch.setattr(tm._replace_tab_bar, "setVisible", lambda v: spied.append(v))
    reg = MagicMock()
    reg.get_floating_cards.return_value = {}
    monkeypatch.setattr(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance",
        lambda: reg,
    )
    visible = {"settings"}
    cm = MagicMock()
    cm.is_card_visible.side_effect = lambda cid, wid: cid in visible
    monkeypatch.setattr(
        "app.widgets.cards.card_manager.CardManager.get_instance",
        lambda: cm,
    )

    # 系统设置显示 → 进 open、tab 显示
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert "settings" in tm._replace_open
    assert spied[-1] is True

    # 关闭系统设置（hidden 事件）→ 调度 120ms 去抖（运行时由 CardManager.hide_card 触发）
    visible.discard("settings")
    tm._on_card_visibility_changed({"card_id": "settings", "visible": False})

    # 真实 QTimer 去抖超时 → 移除 tab、tab 栏隐藏（关闭到只剩对话窗口）
    qtbot.wait(250)
    assert "settings" not in tm._replace_open
    assert tm._replace_tab_bar.isVisible() is False
    assert tm._replace_active != CONVERSATION_ID


def test_replace_tab_close_clicked_hides_global_card(qtbot, monkeypatch):
    """点 tab × 关闭内置全局卡（settings）→ 经 CardManager.hide_card 真正隐藏

    回归：之前误用 hide_floating_card_globally（仅对浮动卡有效，对内置全局卡返回
    False），导致「tab 消失但系统卡片仍显示」。内置全局卡必须走 CardManager。
    """
    from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active = None
    tm._replace_timers.clear()
    spied = []
    monkeypatch.setattr(tm._replace_tab_bar, "setVisible", lambda v: spied.append(v))
    reg = MagicMock()
    reg.get_floating_cards.return_value = {}
    monkeypatch.setattr(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance",
        lambda: reg,
    )
    visible = {"settings"}
    cm = MagicMock()
    cm.is_card_visible.side_effect = lambda cid, wid: cid in visible
    monkeypatch.setattr(
        "app.widgets.cards.card_manager.CardManager.get_instance",
        lambda: cm,
    )

    # 系统设置显示 → 进 open
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert "settings" in tm._replace_open

    # 点 tab × 关闭 → 从 open 移除且真正调 CardManager.hide_card(GLOBAL_WINDOW_ID)
    tm._on_replace_tab_close_clicked("settings")
    assert "settings" not in tm._replace_open
    cm.hide_card.assert_called_once_with("settings", GLOBAL_WINDOW_ID)
    # open 空 → tab 栏隐藏（关闭到只剩对话窗口）
    assert spied[-1] is False
    # 确认未误用无效的 hide_floating_card_globally
    reg.hide_floating_card_globally.assert_not_called()
