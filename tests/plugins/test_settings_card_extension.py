# -*- coding: utf-8 -*-
"""D5: 设置卡片扩展点（registry + LLMSettingsCard 消费）

不变量：
- 注册 settings card → get_settings_cards() 返回
- rebuild_plugin_cards：实例化 widget_class 加入分区；无注册时分区隐藏（行为零变化）
- 重建幂等：重复调用不产生重复卡片
"""

import pytest
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry



def _vis_spy(w):
    """记录 setVisible 调用（桩无窗口层级，isVisible 恒 False 不可断言）"""
    orig = w.setVisible
    w._vis_calls = []

    def _setVisible(v):
        w._vis_calls.append(v)
        orig(v)

    w.setVisible = _setVisible
    return w

@pytest.fixture()
def settings_card(qtbot):
    """轻量设置卡骨架：仅插件分区属性 + tab 按钮（避免构造完整 LLMSettingsCard）"""
    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    card = LLMSettingsCard.__new__(LLMSettingsCard)
    card._plugin_cards_widget = _vis_spy(QWidget())
    card._plugin_cards_widget.setLayout(QVBoxLayout())
    card._plugin_cards_layout = card._plugin_cards_widget.layout()
    card._tab_buttons = {"plugins": _vis_spy(QLabel("插件设置"))}
    # rebuild_plugin_cards 现走 _nav_buttons（与 _tab_buttons 同对象，断言两侧一致）
    card._nav_buttons = card._tab_buttons
    # 已构建标志置 True：跳过「未构建 early return」，否则桩上不会真正实例化卡片
    card._plugin_cards_built = True
    card._page_ready = set()
    qtbot.addWidget(card._plugin_cards_widget)
    qtbot.addWidget(card._tab_buttons["plugins"])
    return card


def test_registry_settings_cards(fresh_registry):
    class _FakeCard:
        pass

    fresh_registry.register_settings_card("demo", "card-1", "插件设置卡", _FakeCard)
    cards = fresh_registry.get_settings_cards()
    assert len(cards) == 1
    assert cards[0].card_id == "card-1" and cards[0].widget_class is _FakeCard


def test_rebuild_renders_cards(qtbot, settings_card, fresh_registry):
    """注册卡片 → rebuild 实例化 widget_class（parent 正确）+ 分区与导航项收到显示指令"""
    captured = {}

    class _FakeCard(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            captured["parent"] = parent

    fresh_registry.register_settings_card("demo", "card-1", "插件设置卡", _FakeCard)
    settings_card.rebuild_plugin_cards()

    assert settings_card._plugin_cards_layout.count() == 1
    assert captured["parent"] is settings_card._plugin_cards_widget
    # 分区与导航项均收到「显示」指令
    assert settings_card._plugin_cards_widget._vis_calls[-1] is True
    assert settings_card._tab_buttons["plugins"]._vis_calls[-1] is True


def test_rebuild_hidden_when_empty(qtbot, settings_card, fresh_registry):
    """无注册卡片 → 分区与导航项均隐藏（行为零变化）"""
    settings_card._plugin_cards_widget.setVisible(True)
    settings_card._tab_buttons["plugins"].setVisible(True)
    settings_card.rebuild_plugin_cards()
    assert settings_card._plugin_cards_layout.count() == 0
    assert settings_card._plugin_cards_widget._vis_calls[-1] is False
    assert settings_card._tab_buttons["plugins"]._vis_calls[-1] is False


def test_rebuild_idempotent(qtbot, settings_card, fresh_registry):
    """重建幂等：重复调用不产生重复卡片；清空注册后分区隐藏"""
    class _FakeCard(QWidget):
        pass

    fresh_registry.register_settings_card("demo", "card-1", "卡", _FakeCard)
    settings_card.rebuild_plugin_cards()
    settings_card.rebuild_plugin_cards()
    assert settings_card._plugin_cards_layout.count() == 1

    fresh_registry.unload_plugin("demo")
    settings_card.rebuild_plugin_cards()
    assert settings_card._plugin_cards_layout.count() == 0
    assert settings_card._plugin_cards_widget._vis_calls[-1] is False
    assert settings_card._tab_buttons["plugins"]._vis_calls[-1] is False


def test_rebuild_exception_safe(qtbot, settings_card, fresh_registry):
    """widget_class 构造抛异常 → 跳过该卡，其余正常（毒条目隔离）"""
    class _BoomCard(QWidget):
        def __init__(self, parent=None):
            raise RuntimeError("boom")

    class _GoodCard(QWidget):
        pass

    fresh_registry.register_settings_card("demo", "bad", "坏卡", _BoomCard)
    fresh_registry.register_settings_card("demo", "good", "好卡", _GoodCard)
    settings_card.rebuild_plugin_cards()
    assert settings_card._plugin_cards_layout.count() == 1  # 只有好卡


# ---------- Phase E：section 分区 ----------


class TestSettingsCardSection:
    def test_default_section_plugins(self, fresh_registry):
        class _Card:
            pass

        fresh_registry.register_settings_card("demo", "c1", "T", _Card)
        assert fresh_registry.get_settings_cards()[0].section == "plugins"
        assert fresh_registry.get_region_entries("settings:plugins")

    def test_section_routes_to_region(self, fresh_registry):
        class _Card:
            pass

        fresh_registry.register_settings_card("demo", "c2", "T", _Card, section="appearance")
        assert fresh_registry.get_region_entries("settings:appearance")
        assert not fresh_registry.get_region_entries("settings:plugins")
        # get_settings_cards 返回全量合集（兼容旧消费端）
        assert [c.card_id for c in fresh_registry.get_settings_cards()] == ["c2"]

    def test_invalid_section_rejected(self, fresh_registry):
        class _Card:
            pass

        with pytest.raises(ValueError, match="section"):
            fresh_registry.register_settings_card("demo", "c3", "T", _Card, section="nowhere")


# ---------- 设置大 tab（register_settings_tab）----------

class _FakePage(QWidget):
    """widget_class 契约桩：签名接受 parent（__new__ 桩非有效 QObject，不真正挂载）"""

    def __init__(self, parent=None):
        super().__init__(None)


def _make_sync_stub_card(qtbot):
    """设置大 tab 同步路径的 __new__ 桩：手工预置 sync 依赖的全部属性"""
    from PyQt5.QtWidgets import QFrame, QStackedWidget, QToolButton, QVBoxLayout

    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    card = LLMSettingsCard.__new__(LLMSettingsCard)
    card._plugin_tab_infos = {}
    card._plugin_tab_pages = {}
    card._current_tab = "plugins"
    card._page_ready = set()
    card._nav_buttons = {}
    card._pages_stack = QStackedWidget()
    qtbot.addWidget(card._pages_stack)
    card._page_scrolls = {}
    card._page_layouts = {}
    card._nav_frame = QFrame()
    qtbot.addWidget(card._nav_frame)
    body_layout = QVBoxLayout()
    anchor = QToolButton()
    qtbot.addWidget(anchor)
    body_layout.addWidget(anchor)
    card._nav_buttons["plugins"] = anchor
    card._nav_body_layout = body_layout
    plugins_page = QWidget()
    card._pages_stack.addWidget(plugins_page)  # 桩基线：plugins 页已在 stack（count 基线 1）

    def _fake_make_page():
        inner = QWidget()
        qtbot.addWidget(inner)
        inner_lay = QVBoxLayout(inner)
        page = QWidget()
        qtbot.addWidget(page)
        QVBoxLayout(page).addWidget(inner)
        return page, inner_lay

    card._make_page = _fake_make_page
    return card


class TestSettingsTabRegistry:
    def test_register_and_get(self, fresh_registry):
        fresh_registry.register_settings_tab("demo", "adv", "高级配置", _FakePage)
        tabs = fresh_registry.get_settings_tabs()
        assert len(tabs) == 1
        assert tabs[0].plugin_name == "demo" and tabs[0].tab_id == "adv"
        assert tabs[0].widget_class is _FakePage

    def test_tab_id_namespaced_per_plugin(self, fresh_registry):
        """同 tab_id 不同插件互不覆盖（key = {plugin_name}:{tab_id}）"""
        fresh_registry.register_settings_tab("a", "adv", "A", _FakePage)
        fresh_registry.register_settings_tab("b", "adv", "B", _FakePage)
        assert len(fresh_registry.get_settings_tabs()) == 2

    def test_priority_override_and_sort(self, fresh_registry):
        fresh_registry.register_settings_tab("demo", "adv", "低", _FakePage, priority=0)
        fresh_registry.register_settings_tab("demo", "adv", "高", _FakePage, priority=5)
        fresh_registry.register_settings_tab("demo", "other", "中", _FakePage, priority=2)
        tabs = fresh_registry.get_settings_tabs()
        # 同 key 高优先级覆盖；排序降序
        assert [t.title for t in tabs] == ["高", "中"]

    def test_invalid_tab_id_rejected(self, fresh_registry):
        with pytest.raises(ValueError):
            fresh_registry.register_settings_tab("demo", "", "T", _FakePage)
        with pytest.raises(ValueError):
            fresh_registry.register_settings_tab("demo", "a:b", "T", _FakePage)

    def test_unload_cleans_tabs(self, fresh_registry):
        fresh_registry.register_settings_tab("demo", "adv", "T", _FakePage)
        fresh_registry.unload_plugin("demo")
        assert fresh_registry.get_settings_tabs() == []


class TestSettingsTabSync:
    def test_sync_adds_nav_and_page(self, qtbot, fresh_registry):
        card = _make_sync_stub_card(qtbot)
        fresh_registry.register_settings_tab("demo", "adv", "高级", _FakePage)
        card._sync_plugin_tabs()
        key = "tab:demo:adv"
        assert key in card._nav_buttons
        assert key in card._page_scrolls
        assert card._pages_stack.count() == 2
        # 懒构造：sync 只建骨架，不实例化插件页面
        assert card._plugin_tab_pages == {}
        # 按钮插在「插件」锚点之后
        layout = card._nav_body_layout
        assert layout.indexOf(card._nav_buttons[key]) == layout.indexOf(card._nav_buttons["plugins"]) + 1

    def test_lazy_build_on_first_enter(self, qtbot, fresh_registry):
        card = _make_sync_stub_card(qtbot)
        fresh_registry.register_settings_tab("demo", "adv", "高级", _FakePage)
        card._sync_plugin_tabs()
        card._ensure_page_ready("tab:demo:adv")
        assert "tab:demo:adv" in card._plugin_tab_pages
        assert card._page_layouts["tab:demo:adv"].count() == 2  # widget + stretch
        # 就绪标记已置位（第二次进入不再重建）
        assert "tab:demo:adv" in card._page_ready

    def test_sync_removes_and_falls_back_to_plugins(self, qtbot, fresh_registry):
        card = _make_sync_stub_card(qtbot)
        fresh_registry.register_settings_tab("demo", "adv", "高级", _FakePage)
        card._sync_plugin_tabs()
        key = "tab:demo:adv"
        card._current_tab = key  # 模拟用户停留在该页
        fresh_registry.unload_plugin("demo")
        card._sync_plugin_tabs()
        assert key not in card._nav_buttons
        assert key not in card._page_scrolls
        assert card._current_tab == "plugins"
        assert card._pages_stack.count() == 1
        assert key not in card._page_ready

    def test_sync_idempotent(self, qtbot, fresh_registry):
        card = _make_sync_stub_card(qtbot)
        fresh_registry.register_settings_tab("demo", "adv", "高级", _FakePage)
        card._sync_plugin_tabs()
        card._sync_plugin_tabs()
        assert card._pages_stack.count() == 2
        assert len(card._nav_buttons) == 2

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
