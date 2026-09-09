# -*- coding: utf-8 -*-
"""组件总项开关后细项列表时序回归测试（2026-08-30 用户报告）

Bug 症状：设置页「工具启用」卡里，关闭某插件的组件总项后细项列表是全量
（正确）；重新开启总项后细项列表变空/不全。

根因：细项行的枚举数据源是 ToolRegistry，而组件「开启」的热重载在
QTimer.singleShot(0) 之后才把工具重新注册回 registry——toggle 的同步路径
上枚举必为空；热重载完成后 _run_hot_reload 又以无参调用
_refresh_after_reload()，把 _defer_hot_reload 存好的 _rebuild_target 丢掉，
错失唯一的修正时机。

本测试锁定修复后的三个行为：
1. 开启方向 toggle 不再同步重建细项行（此刻 registry 还是空的，重建只会
   得到空列表），行保留原状，等热重载完成后按 _rebuild_target 重建；
2. _run_hot_reload 把 _rebuild_target 传给 _refresh_after_reload——开启
   方向的细项行在热重载完成后用恢复后的 registry 重建为全量；
3. _refresh_after_reload 只在目标组件当前「启用」时才重建——关闭方向的
   热重载会把工具从 registry 注销，此时枚举拿不到全量，重建会把 toggle
   同步路径建好的全量列表冲成空列表。

运行: python -m pytest tests/test_plugin_components_card_toggle.py -v
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.plugins.component_items import ComponentItem  # noqa: E402
from app.widgets.cards.settings.plugin_components_card import (  # noqa: E402
    PluginComponentsCard,
    PluginSectionWidget,
)

_PLUGIN, _COMP = "toggle-regress", "tools"


def _pump(app, n=30):
    """驱动事件循环（QTimer.singleShot(0) 的热重载链路需要）"""
    for _ in range(n):
        app.processEvents()


class _State(dict):
    """fake 协作方的可变状态容器"""


@pytest.fixture
def env(monkeypatch, qapp):
    """隔离 PluginManager / PluginHostService / token 估算，卡片可独立驱动

    Returns:
        (card, state, fake_pm)；state["items"] 控制 _component_items 枚举结果，
        state["component_enabled"] 是 fake pm 的组件启停（配置口径）
    """
    state = _State(
        component_enabled=True,
        items=[ComponentItem(id="t1", label="工具一"), ComponentItem(id="t2", label="工具二")],
    )

    fake_pm = MagicMock()
    fake_pm.is_component_enabled.side_effect = lambda p, c: state["component_enabled"]
    fake_pm.set_component_enabled.side_effect = lambda p, c, enabled: state.__setitem__("component_enabled", enabled)
    fake_pm.disabled_keys.return_value = frozenset()
    fake_pm.get_enabled_plugins.return_value = []
    fake_pm.disabled_items.return_value = []
    fake_pm.is_item_enabled.side_effect = lambda p, c, i: True
    monkeypatch.setattr("app.plugins.managers.plugin_manager.PluginManager.get_instance", staticmethod(lambda: fake_pm))

    fake_host = MagicMock()

    def _fake_hot_reload(plugin, comp, enabled):
        """对齐真实热重载时机：registry 的增删只发生在这一步。

        state["items"] 模拟 _component_items 的枚举结果——开启方向热重载后
        工具注册回来（枚举恢复全量）；关闭方向工具被注销（枚举变空）。
        """
        if enabled:
            state["items"] = [ComponentItem(id="t1"), ComponentItem(id="t2")]
        else:
            state["items"] = []
        return {comp: True}  # 热重载「成功」语义：至少一项为真

    fake_host.on_plugin_component_toggled.side_effect = _fake_hot_reload
    monkeypatch.setattr("app.core.plugin_host_service.PluginHostService.get_instance", staticmethod(lambda: fake_host))

    monkeypatch.setattr(
        "app.widgets.cards.settings.plugin_components_card.estimate_component_tokens", lambda p, c: (10, 2)
    )

    card = PluginComponentsCard(components=("tools",))
    # _component_items 枚举改由 state["items"] 驱动（真实实现走 ToolRegistry）
    monkeypatch.setattr(card, "_component_items", lambda plugin, comp: list(state["items"]))

    # 注入一个真实小节（真 ComponentRow / load_items / reload_component_items 链路）
    section = PluginSectionWidget(_PLUGIN, "", True, [_COMP])
    card._pool[_PLUGIN] = section

    card.show()
    section.show()
    _pump(qapp, 5)

    yield card, state, fake_pm

    card.hide()
    section.hide()
    card.deleteLater()
    section.deleteLater()
    _pump(qapp, 5)


def _row(card):
    return card._pool[_PLUGIN].component_row(_COMP)


def _item_count(card):
    return len(_row(card)._item_rows)


class TestComponentToggleItemSync:
    """组件总项开关 ↔ 细项列表同步时序"""

    def test_enable_rebuilds_full_list_after_hot_reload(self, env, qapp):
        """开启总项：同步路径不清空列表，热重载完成后重建为全量（回归点 1+2）

        修复前：同步 reload 在空 registry 上枚举 → 列表被清成 0 行；
        热重载后 _refresh_after_reload 无参调用不重建 → 永久 0 行。
        """
        card, state, _pm = env
        state["component_enabled"] = False
        state["items"] = [ComponentItem(id="stale")]  # 禁用期 registry 空，枚举只剩遗留项
        section = card._pool[_PLUGIN]
        section.set_component_expanded(_COMP, True)  # 用户展开着细项列表才看得到本 bug
        _row(card).load_items(list(state["items"]), lambda _: True)
        _pump(qapp, 3)
        assert _item_count(card) == 1

        # toggle 同步时刻枚举仍是 stale（registry 要等热重载才恢复）
        card._on_component_toggled(_PLUGIN, _COMP, True)
        _pump(qapp)

        # 热重载后按 _rebuild_target 重建 → 全量 2 行（修复前 1 行 stale）
        assert _item_count(card) == 2

    def test_disable_keeps_full_list_after_hot_reload(self, env, qapp):
        """关闭总项：热重载注销工具后不重建，同步建好的全量列表保留（回归点 3）

        修复前（若 _run_hot_reload 正确传参但无启用检查）：关闭方向热重载
        后枚举为空，重建会把全量列表冲成 0 行。
        """
        card, state, _pm = env
        state["component_enabled"] = True
        state["items"] = [ComponentItem(id="t1"), ComponentItem(id="t2")]
        section = card._pool[_PLUGIN]
        section.set_component_expanded(_COMP, True)
        section.load_component_items(_COMP, list(state["items"]), lambda _: True)
        _pump(qapp, 3)
        assert _item_count(card) == 2

        # 同步 reload 仍是全量（此刻热重载未跑，registry 尚未注销）
        card._on_component_toggled(_PLUGIN, _COMP, False)
        assert _item_count(card) == 2

        # 热重载把工具注销（side_effect 已把枚举翻成空）；组件已禁用 → 不得重建
        _pump(qapp)
        assert state["items"] == []
        assert _item_count(card) == 2

    def test_hot_reload_consumes_rebuild_target(self, env, qapp):
        """_run_hot_reload 必须把 _rebuild_target 传给 _refresh_after_reload（回归点 2）

        修复前无参调用，_rebuild_target 是存而不读的死变量。
        """
        card, _state, _pm = env
        received = []
        original = card._refresh_after_reload
        card._refresh_after_reload = lambda items=None: received.append(items)
        try:
            card._defer_hot_reload(lambda: None, rebuild_items=(_PLUGIN, _COMP))
            _pump(qapp)
        finally:
            card._refresh_after_reload = original

        assert (_PLUGIN, _COMP) in received

    def test_item_toggle_does_not_rebuild_rows(self, env, qapp):
        """细项级开关不重建细项行（既有行为保护：避免整片开关闪烁）"""
        card, state, _pm = env
        state["component_enabled"] = True
        section = card._pool[_PLUGIN]
        section.set_component_expanded(_COMP, True)
        section.load_component_items(_COMP, list(state["items"]), lambda _: True)
        _pump(qapp, 3)

        ids_before = [id(r) for r in _row(card)._item_rows.values()]
        card._on_item_toggled(_PLUGIN, _COMP, "t1", False)
        _pump(qapp)

        ids_after = [id(r) for r in _row(card)._item_rows.values()]
        assert ids_before == ids_after
