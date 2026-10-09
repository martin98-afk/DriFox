# -*- coding: utf-8 -*-
"""回归：添加服务商卡片墙的深色适配与标题栏 tab

三个已修缺陷（用户实测 2026-10-09）：
1. 卡片墙在浅色主题下构建后切深色 → 整面白卡不跟随
   （ProviderPickerCard 缺 refresh_style，不在主题刷新链里）
2. 「添加服务商」不在标题栏显示对应 tab
   （provider_picker 未注册进 KNOWN_GLOBAL_REPLACE_CARDS）
3. 服务商编辑卡内模型表头不随主题变色
   （QSS 写了 QWidget#modelTableHeader 但从未 setObjectName）
"""

import sys
from pathlib import Path

import pytest

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="module")
def _qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


@pytest.fixture()
def registry(monkeypatch):
    reg = ProviderRegistry()
    monkeypatch.setattr(ProviderRegistry, "_instance", reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
    reg.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
    return reg


# ══════════════════════════════════════════════════════════════════
# 1. 卡片墙主题跟随
# ══════════════════════════════════════════════════════════════════


class TestPickerThemeFollow:
    def test_card_exposes_refresh_style(self, _qapp, registry):
        """卡片墙必须实现 refresh_style —— 宿主 BaseSettingsCard 的
        _refresh_content_children 按 hasattr 级联，缺了它就永远停在构建时主题"""
        from app.widgets.cards.settings.provider_picker_card import ProviderPickerCard

        card = ProviderPickerCard()
        assert hasattr(card, "refresh_style"), "ProviderPickerCard 必须能被主题刷新链级联到"

    def test_tile_style_rebuilt_on_refresh(self, _qapp, registry, monkeypatch):
        """refresh_style 后 tile 背景/文字取当前主题 token（不是构建时那份）"""
        from app.utils.design_tokens import Colors
        from app.widgets.cards.settings.provider_picker_card import ProviderPickerCard

        card = ProviderPickerCard()
        tile = card.tiles()[0]

        stale_bg = tile.styleSheet()
        stale_name = tile.nameLabel.styleSheet()

        # 篡改 token 后再 refresh：样式串必须用新值重建
        monkeypatch.setattr(Colors, "CARD_BG", "rgba(1, 2, 3, {alpha})", raising=False)
        monkeypatch.setattr(Colors, "TEXT_PRIMARY", "#abcdef", raising=False)
        card.refresh_style()

        assert tile.styleSheet() != stale_bg, "refresh_style 未重建 tile 卡片样式"
        assert "1, 2, 3" in tile.styleSheet()
        assert tile.nameLabel.styleSheet() != stale_name
        assert "#abcdef" in tile.nameLabel.styleSheet()

    def test_group_header_style_rebuilt_on_refresh(self, _qapp, registry, monkeypatch):
        """组头（色条 + 组名标签）也要跟随主题重建"""
        from app.utils.design_tokens import Colors
        from app.widgets.cards.settings.provider_picker_card import ProviderPickerCard

        card = ProviderPickerCard()
        assert card._headers, "组头引用必须记录，否则 refresh 无从重刷"
        _anchor, label, _color = card._headers[0]
        stale = label.styleSheet()

        monkeypatch.setattr(Colors, "TEXT_MUTED", "#123456", raising=False)
        card.refresh_style()

        assert label.styleSheet() != stale
        assert "#123456" in label.styleSheet()

    def test_custom_entry_header_follows_theme(self, _qapp, registry, monkeypatch):
        """「其他（1）」组头是唯一传 color=None 的（跟随 TEXT_MUTED），须一并刷新"""
        from app.utils.design_tokens import Colors
        from app.widgets.cards.settings.provider_picker_card import ProviderPickerCard

        card = ProviderPickerCard()
        _anchor, label, color = card._headers[-1]
        assert color is None, "自定义组头应传 None 走主题 token"

        monkeypatch.setattr(Colors, "TEXT_MUTED", "#654321", raising=False)
        card.refresh_style()
        assert "#654321" in _anchor.styleSheet()
        assert "#654321" in label.styleSheet()


# ══════════════════════════════════════════════════════════════════
# 2. 标题栏 tab 注册
# ══════════════════════════════════════════════════════════════════


class TestPickerTitlebarTab:
    def test_picker_registered_as_global_replace_card(self):
        """provider_picker 须在全局替换卡白名单，否则显隐事件被直接忽略"""
        from app.widgets.tab_manager_window import GLOBAL_REPLACE_TITLES, KNOWN_GLOBAL_REPLACE_CARDS

        assert "provider_picker" in KNOWN_GLOBAL_REPLACE_CARDS
        assert GLOBAL_REPLACE_TITLES["provider_picker"] == "添加服务商"

    def test_visibility_event_adds_tab(self, qtbot, monkeypatch):
        """卡片显示 → 标题栏出现「添加服务商」tab"""
        from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID
        from app.widgets.tab_manager_window import TabManagerWindow

        TabManagerWindow._instance = None
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        tm._replace_open.clear()
        tm._replace_active.clear()
        tm._replace_timers.clear()

        from unittest.mock import MagicMock

        reg = MagicMock()
        reg.get_floating_cards.return_value = {}
        monkeypatch.setattr("app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance", lambda: reg)
        cm = MagicMock()
        cm.is_card_visible.side_effect = lambda cid, wid: False
        monkeypatch.setattr("app.widgets.cards.card_manager.CardManager.get_instance", lambda: cm)

        try:
            tm._on_card_visibility_changed({"card_id": "provider_picker", "visible": True})
            assert tm._replace_open.get(GLOBAL_WINDOW_ID, {}).get("provider_picker") == "添加服务商"
            assert tm.titleBar._tabs["provider_picker"]._label.text() == "添加服务商"
        finally:
            for t in list(tm._replace_timers.values()):
                t.stop()
            TabManagerWindow._instance = None


# ══════════════════════════════════════════════════════════════════
# 3. 模型表头主题色
# ══════════════════════════════════════════════════════════════════


class TestModelTableHeaderTheme:
    def test_header_has_object_name(self, _qapp):
        """表头必须带 modelTableHeader objectName —— QSS 选择器的唯一锚点"""
        from app.widgets.model_list_edit_dialog import ModelListEditorWidget

        editor = ModelListEditorWidget(models=["deepseek-v4-flash"])
        assert editor.headerWidget.objectName() == "modelTableHeader"

    def test_header_labels_get_theme_color(self, _qapp):
        """objectName 设好后，QSS 规则命中 → 表头文字取 TEXT_MUTED（非系统默认黑）"""
        from PyQt5.QtGui import QPalette
        from PyQt5.QtWidgets import QLabel

        from app.utils.design_tokens import Colors
        from app.widgets.model_list_edit_dialog import ModelListEditorWidget

        Colors.refresh()
        editor = ModelListEditorWidget(models=["deepseek-v4-flash"])
        editor.show()
        _qapp.processEvents()

        try:
            labels = editor.headerWidget.findChildren(QLabel)
            assert labels, "表头应有列标签"
            for lb in labels:
                got = lb.palette().color(QPalette.WindowText).name()
                assert got == Colors.TEXT_MUTED, f"表头 {lb.text()!r} 未取主题色: {got}"
        finally:
            editor.hide()
