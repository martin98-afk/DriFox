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
    """轻量设置卡骨架：插件分区可见性 spy + 假插件页（避免构造完整 LLMSettingsCard）

    2026-11-07 起插件页改为 PluginSettingsPage（主从结构），rebuild_plugin_cards
    不再实例化各插件设置卡，改为刷新页面；自定义设置卡在页面详情里实例化，
    由 tests/widgets/test_plugin_settings_page.py 覆盖。
    """
    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    class _FakePage:
        def __init__(self):
            self.reloads = 0
            self.last_force = None

        def reload(self, force=False):
            self.reloads += 1
            self.last_force = force

    card = LLMSettingsCard.__new__(LLMSettingsCard)
    card._plugin_cards_widget = _vis_spy(QWidget())
    card._plugin_cards_widget.setLayout(QVBoxLayout())
    card._tab_buttons = {"plugins": _vis_spy(QLabel("插件设置"))}
    card._nav_buttons = card._tab_buttons
    card._plugin_cards_built = True
    card._page_ready = set()
    card.pluginSettingsPage = _FakePage()
    card._sync_plugin_tabs = lambda: None
    card._has_plugins = True
    card._sync_plugin_nav_visibility = lambda: _apply_visibility(card)
    qtbot.addWidget(card._plugin_cards_widget)
    qtbot.addWidget(card._tab_buttons["plugins"])
    return card


def _apply_visibility(card):
    """按 card._has_plugins 同步可见性，语义与真实实现一致"""
    card._plugin_cards_widget.setVisible(card._has_plugins)
    card._tab_buttons["plugins"].setVisible(card._has_plugins)
    return card._has_plugins


def test_registry_settings_cards(fresh_registry):
    class _FakeCard:
        pass

    fresh_registry.register_settings_card("demo", "card-1", "插件设置卡", _FakeCard)
    cards = fresh_registry.get_settings_cards()
    assert len(cards) == 1
    assert cards[0].card_id == "card-1" and cards[0].widget_class is _FakeCard


def test_rebuild_refreshes_plugin_page(qtbot, settings_card, fresh_registry):
    """有插件 → 刷新插件页 + 分区与导航项收到显示指令"""
    settings_card.rebuild_plugin_cards(force=True)
    assert settings_card.pluginSettingsPage.reloads == 1
    assert settings_card.pluginSettingsPage.last_force is True
    assert settings_card._plugin_cards_widget._vis_calls[-1] is True
    assert settings_card._tab_buttons["plugins"]._vis_calls[-1] is True


def test_rebuild_hidden_when_no_plugins(qtbot, settings_card, fresh_registry):
    """无插件 → 分区与导航项均隐藏，且不触发页面刷新"""
    settings_card._has_plugins = False
    settings_card._plugin_cards_widget.setVisible(True)
    settings_card._tab_buttons["plugins"].setVisible(True)
    settings_card.rebuild_plugin_cards(force=True)
    assert settings_card.pluginSettingsPage.reloads == 0
    assert settings_card._plugin_cards_widget._vis_calls[-1] is False
    assert settings_card._tab_buttons["plugins"]._vis_calls[-1] is False


def test_rebuild_idempotent(qtbot, settings_card, fresh_registry):
    """重复调用不重复刷新副作用；插件清空后分区隐藏"""
    settings_card.rebuild_plugin_cards(force=True)
    settings_card.rebuild_plugin_cards(force=True)
    assert settings_card.pluginSettingsPage.reloads == 2

    settings_card._has_plugins = False
    settings_card.rebuild_plugin_cards(force=True)
    assert settings_card._plugin_cards_widget._vis_calls[-1] is False
    assert settings_card._tab_buttons["plugins"]._vis_calls[-1] is False


def test_rebuild_exception_safe(qtbot, settings_card, fresh_registry):
    """页面刷新抛异常 → 不向上传播（设置卡其他分区不受影响）"""
    def _boom(force=False):
        raise RuntimeError("boom")

    settings_card.pluginSettingsPage.reload = _boom
    settings_card.rebuild_plugin_cards(force=True)  # 不应抛异常
    assert settings_card._plugin_cards_widget._vis_calls[-1] is True


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

class TestFullwidthMode:
    """插件类页面独占全宽（2026-11-08 设计）：进插件页收起左导航，返回按钮退出

    痛点：设置导航(150~196px) + 插件列表(236px) + 详情主从并存，四栏挤压详情。
    """

    def _make_fullwidth_stub(self, qtbot):
        from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

        card = LLMSettingsCard.__new__(LLMSettingsCard)
        card._nav_frame = _vis_spy(QWidget())
        card._back_btn = _vis_spy(QWidget())
        qtbot.addWidget(card._nav_frame)
        qtbot.addWidget(card._back_btn)
        return card

    def test_fullwidth_predicate(self):
        from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

        assert LLMSettingsCard._is_fullwidth_tab("plugins") is True
        assert LLMSettingsCard._is_fullwidth_tab("tab:git-panel:main") is True
        assert LLMSettingsCard._is_fullwidth_tab("provider") is False
        assert LLMSettingsCard._is_fullwidth_tab("plugins_extra") is False  # 仅精确匹配

    def test_enter_plugins_hides_nav(self, qtbot):
        card = self._make_fullwidth_stub(qtbot)
        card._apply_fullwidth_mode("plugins")
        assert card._nav_frame._vis_calls[-1] is False
        assert card._back_btn._vis_calls[-1] is True

    def test_plugin_registered_tab_also_fullwidth(self, qtbot):
        card = self._make_fullwidth_stub(qtbot)
        card._apply_fullwidth_mode("tab:demo:adv")
        assert card._nav_frame._vis_calls[-1] is False
        assert card._back_btn._vis_calls[-1] is True

    def test_normal_tab_restores_nav(self, qtbot):
        card = self._make_fullwidth_stub(qtbot)
        card._apply_fullwidth_mode("provider")
        assert card._nav_frame._vis_calls[-1] is True
        assert card._back_btn._vis_calls[-1] is False

    def test_exit_restores_nav(self, qtbot):
        card = self._make_fullwidth_stub(qtbot)
        card._apply_fullwidth_mode("plugins")
        card._exit_fullwidth_mode()
        assert card._nav_frame._vis_calls[-1] is True
        assert card._back_btn._vis_calls[-1] is False

    def test_stub_without_widgets_safe(self, qtbot):
        """__new__ 桩缺 _nav_frame/_back_btn 时收起/恢复不抛异常"""
        from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

        card = LLMSettingsCard.__new__(LLMSettingsCard)
        card._apply_fullwidth_mode("plugins")
        card._exit_fullwidth_mode()

if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
