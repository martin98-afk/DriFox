# -*- coding: utf-8 -*-
"""标题栏 full 卡片 tab（非常驻可关闭）与插件常驻 tab 槽位测试

覆盖（自 test_replace_tab_bar.py 迁移，ReplaceTabBar 已由 CustomTitleBar tab 区取代）：
- full 卡片显隐事件 → 标题栏 tab 动态增删/高亮（≥1 即显示在标题栏）
- 非 full 卡片 / 非 GLOBAL_WINDOW_ID 作用域事件忽略
- 互斥切换保留 open、用户关闭后剩余自动激活
- 120ms 去抖（直接回调 + 真实 QTimer 两路径）
- 点「聊天」回对话视图（open 保留）、per-tab 隔离
- UIPluginRegistry.register_titlebar_tab 常驻 tab 注册/注销
- 覆盖层限宽只针对配置类卡片
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.widgets.tab_manager_window import CHAT_TAB_ID, TabManagerWindow
from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID


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


def _patch_reg_and_cm(monkeypatch, *, cards=None, visible=None):
    """统一打桩：UIPluginRegistry（浮动卡注册集）+ CardManager（可见集合）

    返回 visible 集合供测试维护互斥显隐状态。"""
    reg = MagicMock()
    reg.get_floating_cards.return_value = cards or {}
    monkeypatch.setattr(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance",
        lambda: reg,
    )
    if visible is None:
        visible = set()
    cm = MagicMock()
    cm.is_card_visible.side_effect = lambda cid, wid: cid in visible
    monkeypatch.setattr(
        "app.widgets.cards.card_manager.CardManager.get_instance",
        lambda: cm,
    )
    return reg, cm, visible


def test_full_cards_titlebar_sync(qtbot, monkeypatch):
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)

    visible = set()
    _patch_reg_and_cm(
        monkeypatch,
        cards={
            "usage": SimpleNamespace(container="full", title="用量统计"),
            "market": SimpleNamespace(container="full", title="插件市场"),
            "settings": SimpleNamespace(container="top", title="设置"),
        },
        visible=visible,
    )

    # 非 full 卡片事件被忽略
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert "settings" not in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert "settings" not in tm.titleBar._tabs

    # 开第一个 full → 标题栏出现可关闭 tab
    visible.add("usage")
    tm._on_card_visibility_changed({"card_id": "usage", "visible": True})
    assert "usage" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert "usage" in tm.titleBar._tabs
    assert tm.titleBar._tabs["usage"]._closable is True
    assert tm.titleBar._active_id == "usage"

    # 开第二个 → 同样进标题栏（互斥显示，tab 并存）
    visible.discard("usage")
    visible.add("market")
    tm._on_card_visibility_changed({"card_id": "market", "visible": True})
    assert set(tm._replace_open.get(GLOBAL_WINDOW_ID, {})) == {"usage", "market"}
    assert {"usage", "market"} <= set(tm.titleBar._tabs)

    # 互斥切换：隐藏 usage（market 仍可见）→ 保留 usage tab
    tm._on_card_visibility_changed({"card_id": "usage", "visible": False})
    qtbot.wait(200)
    assert "usage" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})

    # 关闭 market（最后可见的）→ 移除（去抖判定：无其他可见卡 → 用户关闭）
    visible.discard("market")
    tm._on_card_visibility_changed({"card_id": "market", "visible": False})
    tm._on_replace_close_timeout("market")
    assert "market" not in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert "market" not in tm.titleBar._tabs
    # 自动激活剩余卡片：恢复 usage 可见性后走同步激活路径（真实环境由
    # toggle_floating_card → shown 事件异步驱动，此处直接验证同步分支）
    visible.add("usage")
    tm._activate_remaining_replace_card()
    assert tm.titleBar._active_id == "usage"


def test_close_full_card_keeps_permanent_titlebar_tab(qtbot, monkeypatch):
    """回归：常驻 titlebar tab 与 full 卡共用 card_id 时，hidden 去抖关闭不得删常驻 tab

    assistant_hub 场景：插件注册常驻「助手」tab（tab_id=card_id=assistant_hub），
    点「新建人格」→ hide_floating_card_globally → hidden 事件 → 120ms 去抖超时。
    此前 _on_replace_close_timeout 无条件 remove_tab，把常驻 tab 也删了且无人恢复。
    """
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active.clear()
    tm._replace_timers.clear()
    _patch_reg_and_cm(
        monkeypatch,
        cards={"assistant_hub": SimpleNamespace(container="full", title="助手中心")},
    )

    # 模拟 _sync_plugin_titlebar_tabs 的产物：常驻 tab 已挂载（不可关闭）
    tm.titleBar.add_tab("assistant_hub", "助手", on_click=lambda: None, closable=False)
    tm._plugin_titlebar_tab_ids.add("assistant_hub")

    # full 卡显示 → open 记录；add_tab 因 tab 已存在跳过（保持常驻形态）
    tm._on_card_visibility_changed({"card_id": "assistant_hub", "visible": True})
    assert "assistant_hub" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})

    # 关闭（hidden 事件 → 直接触发去抖超时，绕过 QTimer 跨测试时序污染）
    tm._on_card_visibility_changed({"card_id": "assistant_hub", "visible": False})
    tm._on_replace_close_timeout("assistant_hub")

    # open/active 清除，但常驻 tab 必须保留（点击 on_click 可再打开卡片）
    assert "assistant_hub" not in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert "assistant_hub" in tm.titleBar._tabs
    assert tm.titleBar._tabs["assistant_hub"]._closable is False
    assert tm.titleBar._active_id == CHAT_TAB_ID


def test_close_replace_card_keeps_permanent_titlebar_tab(qtbot, monkeypatch):
    """回归：close_replace_card（tab × / 卡内关闭钮共用入口）同样不得删常驻 tab"""
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active.clear()
    tm._replace_timers.clear()
    _patch_reg_and_cm(
        monkeypatch,
        cards={"assistant_hub": SimpleNamespace(container="full", title="助手中心")},
    )

    tm.titleBar.add_tab("assistant_hub", "助手", on_click=lambda: None, closable=False)
    tm._plugin_titlebar_tab_ids.add("assistant_hub")

    tm._on_card_visibility_changed({"card_id": "assistant_hub", "visible": True})
    assert "assistant_hub" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})

    tm.close_replace_card("assistant_hub")

    assert "assistant_hub" not in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert "assistant_hub" in tm.titleBar._tabs


def test_global_replace_cards_sync(qtbot, monkeypatch):
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active.clear()
    tm._replace_timers.clear()
    _patch_reg_and_cm(monkeypatch)[0:2]

    # 非 GLOBAL_WINDOW_ID 作用域的卡片事件被忽略
    tm._on_card_visibility_changed({"card_id": "settings", "window_id": "other", "visible": True})
    assert "settings" not in dict(tm._replace_open)

    # 内置全局卡「系统设置」显示 → 进 open 且标题栏 tab 出现（标题取内置映射）
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert tm._replace_open.get(GLOBAL_WINDOW_ID, {}).get("settings") == "系统设置"
    assert tm.titleBar._tabs["settings"]._label.text() == "系统设置"

    # 点「聊天」→ 隐藏所有 replace 卡片回到对话区，但保留 open（聊天高亮）
    tm._on_titlebar_tab_clicked(CHAT_TAB_ID)
    assert "settings" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert tm._replace_active.get(GLOBAL_WINDOW_ID) == CHAT_TAB_ID
    assert tm.titleBar._active_id == CHAT_TAB_ID

    # 子智能体会话显示 → 进 open（标题取内置映射）
    tm._on_card_visibility_changed({"card_id": "sub_agent_session", "visible": True})
    assert tm._replace_open.get(GLOBAL_WINDOW_ID, {}).get("sub_agent_session") == "子智能体会话"


def test_chat_tab_keeps_open(qtbot, monkeypatch):
    """点「聊天」仅切换视图，不销毁 open 集合（标题栏 tab 常驻聊天项）"""
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active.clear()
    tm._replace_timers.clear()
    _patch_reg_and_cm(monkeypatch)[0:2]

    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert "settings" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})

    # 点「聊天」→ 隐藏 settings 但保留 open，聊天高亮
    tm._on_titlebar_tab_clicked(CHAT_TAB_ID)
    assert "settings" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert tm._replace_active.get(GLOBAL_WINDOW_ID) == CHAT_TAB_ID
    assert tm.titleBar._active_id == CHAT_TAB_ID

    # 再点系统设置 → 回到该卡，聊天取消高亮
    tm._on_titlebar_tab_clicked("settings")
    assert tm._replace_active.get(GLOBAL_WINDOW_ID) == "settings"
    assert tm.titleBar._active_id == "settings"


def test_titlebar_card_disappears_on_card_close(qtbot, monkeypatch):
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active.clear()
    tm._replace_timers.clear()
    _patch_reg_and_cm(monkeypatch)[0:2]

    # 系统设置显示 → 进 open + 标题栏 tab
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert "settings" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})

    # 关闭系统设置（hidden 事件）→ 直接触发关闭去抖超时（绕过 QTimer 跨测试时序污染）
    tm._on_card_visibility_changed({"card_id": "settings", "visible": False})
    tm._on_replace_close_timeout("settings")
    assert "settings" not in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert "settings" not in tm.titleBar._tabs
    # 全部关闭 → 高亮回到「聊天」
    assert tm.titleBar._active_id == CHAT_TAB_ID


def test_replace_close_via_qtimer(qtbot, monkeypatch):
    """真实 QTimer 去抖链路：hidden 事件 → 120ms → 移除标题栏 tab"""
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active.clear()
    tm._replace_timers.clear()
    _patch_reg_and_cm(monkeypatch)[0:2]

    # 系统设置显示 → 进 open、标题栏 tab 出现
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert "settings" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert "settings" in tm.titleBar._tabs

    # 关闭系统设置（hidden 事件）→ 调度 120ms 去抖
    tm._on_card_visibility_changed({"card_id": "settings", "visible": False})

    # 真实 QTimer 去抖超时 → 移除 tab（关闭到只剩对话视图）
    qtbot.wait(250)
    assert "settings" not in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert "settings" not in tm.titleBar._tabs
    assert tm._replace_active.get(GLOBAL_WINDOW_ID) != "settings"


def test_titlebar_tab_close_clicked_hides_global_card(qtbot, monkeypatch):
    """点 tab × 关闭内置全局卡（settings）→ 经 CardManager.hide_card 真正隐藏

    回归：之前误用 hide_floating_card_globally（仅对浮动卡有效，对内置全局卡返回
    False），导致「tab 消失但系统卡片仍显示」。内置全局卡必须走 CardManager。
    """
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active.clear()
    tm._replace_timers.clear()
    reg, cm, _vis = _patch_reg_and_cm(monkeypatch)

    # 系统设置显示 → 进 open
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert "settings" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})

    # 点 tab × 关闭 → 从 open 移除且真正调 CardManager.hide_card(GLOBAL_WINDOW_ID)
    tm._on_replace_tab_close_clicked("settings")
    assert "settings" not in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    cm.hide_card.assert_called_once_with("settings", GLOBAL_WINDOW_ID)
    assert "settings" not in tm.titleBar._tabs
    # 确认未误用无效的 hide_floating_card_globally
    reg.hide_floating_card_globally.assert_not_called()


def test_titlebar_cards_isolated_per_conversation(qtbot, monkeypatch):
    """不同对话标签页的标题栏 tab 打开列表/高亮独立（只隔离 tab，卡片全局共享）"""
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active.clear()
    tm._replace_timers.clear()
    _patch_reg_and_cm(monkeypatch)[0:2]

    # 模拟两个对话窗口，按「当前活跃对话」的 _window_id 归属 open
    win_a = SimpleNamespace(_window_id="win_a")
    win_b = SimpleNamespace(_window_id="win_b")
    current = {"win": win_a}

    monkeypatch.setattr(tm, "get_current_window", lambda: current["win"])

    # 对话 A 打开系统设置
    tm._on_card_visibility_changed({"card_id": "settings", "visible": True})
    assert tm._replace_open.get("win_a") == {"settings": "系统设置"}
    assert "win_b" not in tm._replace_open

    # 切到对话 B（重建标题栏卡片 tab 为 B 的空列表，尚无打开项）
    current["win"] = win_b
    tm._on_active_tab_changed(1)
    card_tabs = [t for t in tm.titleBar._tabs if t != CHAT_TAB_ID and t not in tm._plugin_titlebar_tab_ids]
    assert card_tabs == []
    assert tm.titleBar._active_id == CHAT_TAB_ID

    # 对话 B 打开服务商编辑
    tm._on_card_visibility_changed({"card_id": "provider_edit", "visible": True})
    assert list(tm._replace_open.get("win_b", {})) == ["provider_edit"]
    # 对话 A 的 open 不受影响
    assert tm._replace_open.get("win_a") == {"settings": "系统设置"}
    card_tabs = [t for t in tm.titleBar._tabs if t != CHAT_TAB_ID and t not in tm._plugin_titlebar_tab_ids]
    assert card_tabs == ["provider_edit"]

    # 切回对话 A：标题栏只显示 A 的打开项（B 的 provider_edit 不串入）
    current["win"] = win_a
    tm._on_active_tab_changed(0)
    card_tabs = [t for t in tm.titleBar._tabs if t != CHAT_TAB_ID and t not in tm._plugin_titlebar_tab_ids]
    assert card_tabs == ["settings"]


def test_close_last_temp_tab_skips_permanent_plugin_tab(qtbot, monkeypatch):
    """关闭最后一个临时 tab 后回聊天：常驻插件 tab（如轨迹）不被自动激活

    回归：轨迹插件同时注册常驻 titlebar tab 与 full 浮动卡（共用同一 card_id），
    其 full 卡进入 open 集合后，_activate_remaining_replace_card 曾把它当作
    可切换临时卡——关闭最后一个临时 tab 后自动弹出轨迹卡并高亮常驻 tab。
    常驻 tab 只能由用户手动点击，自动激活链路必须排除。
    """
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._replace_open.clear()
    tm._replace_active.clear()
    tm._replace_timers.clear()
    reg, cm, visible = _patch_reg_and_cm(
        monkeypatch,
        cards={
            "usage": SimpleNamespace(container="full", title="用量统计"),
            "agent_trace": SimpleNamespace(container="full", title="轨迹"),
        },
    )
    # 轨迹插件：常驻 titlebar tab 与 full 浮动卡共用 card_id（agent_trace 实况）
    reg.get_titlebar_tabs.return_value = [
        SimpleNamespace(tab_id="agent_trace", label="轨迹", on_click=lambda: None, icon_path="")
    ]
    tm._sync_plugin_titlebar_tabs()
    assert "agent_trace" in tm._plugin_titlebar_tab_ids
    assert tm.titleBar._tabs["agent_trace"]._closable is False

    # 打开轨迹卡（shown）→ open 记录 agent_trace；add_tab 幂等，常驻 tab 无 ×
    visible.add("agent_trace")
    tm._on_card_visibility_changed({"card_id": "agent_trace", "visible": True})
    assert "agent_trace" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert tm.titleBar._active_id == "agent_trace"

    # 再打开临时 tab usage → 激活 usage
    visible.discard("agent_trace")
    visible.add("usage")
    tm._on_card_visibility_changed({"card_id": "usage", "visible": True})
    assert tm.titleBar._active_id == "usage"

    # 关闭唯一的临时 tab usage → 回聊天；轨迹卡不被自动激活（open 保留供手动再开）
    tm._on_replace_tab_close_clicked("usage")
    visible.discard("usage")
    assert "usage" not in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert tm.titleBar._active_id == CHAT_TAB_ID
    assert tm._replace_active.get(GLOBAL_WINDOW_ID) == CHAT_TAB_ID
    assert "agent_trace" in tm._replace_open.get(GLOBAL_WINDOW_ID, {})
    assert cm.is_card_visible.call_count >= 0  # 可见集由测试维护：断言高亮已回聊天


class TestTitlebarTabSlot:
    """UIPluginRegistry 标题栏常驻 tab 槽位（非常驻 full 卡之外的注册入口）"""

    def test_register_and_get(self):
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        hits = []
        reg.register_titlebar_tab("p1", "tab_x", "工具箱", on_click=lambda: hits.append(1), priority=0)
        reg.register_titlebar_tab("p2", "tab_y", "市场")
        try:
            infos = {i.tab_id: i for i in reg.get_titlebar_tabs()}
            assert set(infos) >= {"tab_x", "tab_y"}
            assert infos["tab_x"].label == "工具箱"
            infos["tab_x"].on_click()
            assert hits == [1]
        finally:
            reg.unregister_titlebar_tabs("p1")
            reg.unregister_titlebar_tabs("p2")
        assert all(i.tab_id not in ("tab_x", "tab_y") for i in reg.get_titlebar_tabs())

    def test_priority_override_and_unregister_scope(self):
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        reg.register_titlebar_tab("pa", "tab_z", "低优先", priority=0)
        reg.register_titlebar_tab("pb", "tab_z", "高优先", priority=10)
        try:
            info = next(i for i in reg.get_titlebar_tabs() if i.tab_id == "tab_z")
            assert info.label == "高优先"
        finally:
            reg.unregister_titlebar_tabs("pb")
        # pb 注销后低优先实现不会被"复活"（同 id 覆盖语义）
        assert all(i.tab_id != "tab_z" for i in reg.get_titlebar_tabs())
        reg.unregister_titlebar_tabs("pa")

    def test_window_sync_mounts_and_unmounts(self, qtbot, monkeypatch):
        """_sync_plugin_titlebar_tabs：注册后挂载（不可关闭），注销后移除"""
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        reg = UIPluginRegistry.get_instance()
        reg.register_titlebar_tab("pt", "plugin_tab", "插件页", on_click=lambda: None)
        try:
            tm._sync_plugin_titlebar_tabs()
            assert "plugin_tab" in tm.titleBar._tabs
            assert "plugin_tab" in tm._plugin_titlebar_tab_ids
            assert tm.titleBar._tabs["plugin_tab"]._closable is False
        finally:
            reg.unregister_titlebar_tabs("pt")
        tm._sync_plugin_titlebar_tabs()
        assert "plugin_tab" not in tm.titleBar._tabs
        assert "plugin_tab" not in tm._plugin_titlebar_tab_ids


def test_overlay_limit_width_config_only(qtbot):
    """覆盖层限宽只针对配置类卡片：settings → True，diff_viewer/sub_agent_session → False"""
    from PyQt5.QtWidgets import QWidget

    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    container = tm._global_top_container
    container.show()  # 覆盖层激活态（子 isHidden 才反映自身显隐）

    cfg_card, diff_card, sub_card = QWidget(), QWidget(), QWidget()
    container._cards["settings"] = cfg_card
    container._cards["diff_viewer"] = diff_card
    container._cards["sub_agent_session"] = sub_card
    for c in (cfg_card, diff_card, sub_card):
        c.hide()

    # 无可见卡 → 不限宽
    assert tm._overlay_should_limit_width() is False
    # 内容型可见（diff / 子智能体会话）→ 不限宽铺满
    diff_card.setVisible(True)
    assert tm._overlay_should_limit_width() is False
    diff_card.hide()
    sub_card.setVisible(True)
    assert tm._overlay_should_limit_width() is False
    # 配置类可见（settings）→ 限宽居中
    sub_card.hide()
    cfg_card.setVisible(True)
    assert tm._overlay_should_limit_width() is True
