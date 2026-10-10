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
from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QLabel, QWidget

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
        assert GLOBAL_REPLACE_TITLES["provider_picker"] == "服务商"

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
            assert tm._replace_open.get(GLOBAL_WINDOW_ID, {}).get("provider_picker") == "服务商"
            assert tm.titleBar._tabs["provider_picker"]._label.text() == "服务商"
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


# ══════════════════════════════════════════════════════════════════
# 服务商卡面包屑与卡内导航（2026-10-10 单卡双视图重构）
# ══════════════════════════════════════════════════════════════════


class TestSystemCardFrameBreadcrumb:
    """SystemCardFrame.set_breadcrumb API"""

    def test_set_and_clear(self, _qapp):
        """设置两项 → title 隐藏面包屑显示；清空 → 恢复 title"""
        from app.widgets.cards.settings.base_settings_card import BaseSettingsCard

        card = BaseSettingsCard("服务商", icon_svg="大模型")
        card.show()
        _qapp.processEvents()

        assert not card._breadcrumb_widget.isVisible()
        assert card.title_label.isVisible()

        card.set_breadcrumb([{"text": "服务商", "handler": lambda: None}, {"text": "OpenCode Go"}])
        assert card.title_label.isHidden(), "有面包屑时 title 应隐藏"
        assert card._breadcrumb_widget.isVisible()

        texts = [
            card._breadcrumb_layout.itemAt(i).widget().text()
            for i in range(card._breadcrumb_layout.count())
            if card._breadcrumb_layout.itemAt(i).widget() is not None
        ]
        assert texts == ["服务商", "›", "OpenCode Go"], f"面包屑文本异常: {texts}"

        card.set_breadcrumb([])
        assert card.title_label.isVisible(), "清空后 title 应恢复"
        assert not card._breadcrumb_widget.isVisible()

    def test_ancestor_click_invokes_handler(self, _qapp):
        """点祖先项触发 handler；当前项不可点"""
        from app.widgets.cards.settings.base_settings_card import BaseSettingsCard

        card = BaseSettingsCard("服务商", icon_svg="大模型")
        hits = []
        card.set_breadcrumb([{"text": "服务商", "handler": lambda: hits.append(1)}, {"text": "OpenCode Go"}])
        _qapp.processEvents()

        # 祖先项 = layout 第 0 个 widget
        ancestor = card._breadcrumb_layout.itemAt(0).widget()
        ancestor.mousePressEvent(
            QMouseEvent(QEvent.MouseButtonPress, QPoint(1, 1), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        )
        assert hits == [1]

        # 当前项（第 2 个 widget）没有手型光标（不可点）
        current = card._breadcrumb_layout.itemAt(2).widget()
        assert current.cursor().shape() != Qt.PointingHandCursor

    def test_refresh_restyles(self, _qapp, monkeypatch):
        """主题刷新重刷面包屑颜色"""
        from app.utils.design_tokens import Colors
        from app.widgets.cards.settings.base_settings_card import BaseSettingsCard

        card = BaseSettingsCard("服务商", icon_svg="大模型")
        card.set_breadcrumb([{"text": "服务商", "handler": lambda: None}, {"text": "x"}])
        _qapp.processEvents()

        monkeypatch.setattr(Colors, "TEXT_ACCENT", "#010203", raising=False)
        card.refresh_style()
        ancestor = card._breadcrumb_layout.itemAt(0).widget()
        assert "#010203" in ancestor.styleSheet()

    def test_item_font_matches_title_size(self, _qapp):
        """面包屑项字体必须与 title_label 逐项一致，QSS font-size 不得覆盖

        缺陷史两轮（用户实测）：
        1. 项样式串带 font_size_css(11)（11px）而 title 是 setFont pt 模式 →
           进入面包屑后字体骤小；
        2. 改 setFont(get_unified_font(12)) 后，title 在启动早期取档、面包屑
           打开时取档，字号档位缓存（delta）漂移 → 面包屑比 title 大一点。
        终案：_apply_breadcrumb_styles 先把 title 规格化到当前档，再克隆同一
        QFont 给每项——无论档位何时变化，面包屑与 title 恒等。
        """
        from app.widgets.cards.settings.system_card_frame import SystemCardFrame

        card = SystemCardFrame()
        card.set_title_text("服务商")
        card.set_breadcrumb([{"text": "服务商", "handler": lambda: None}, {"text": "OpenCode Go"}])
        _qapp.processEvents()

        title_font = card.title_label.font()
        for pos in (0, 2):
            item = card._breadcrumb_layout.itemAt(pos).widget()
            assert item.font().pointSize() == title_font.pointSize(), (
                f"面包屑项 {item.font().pointSize()}pt 应与 title {title_font.pointSize()}pt 一致"
            )
            assert item.font().bold() == title_font.bold()
            assert item.font().pixelSize() == title_font.pixelSize()
            assert "font-size" not in item.styleSheet(), "样式串含 font-size 会覆盖 setFont"


class TestProviderCardNav:
    """服务商卡单卡双视图导航（controller 层）"""

    @pytest.fixture()
    def cc(self, _qapp, monkeypatch):
        """构建最小可用的 GlobalCardController（CardManager/注册表打桩）"""
        from unittest.mock import MagicMock

        from app.plugins.registries.provider_registry import ProviderRegistry
        from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID  # noqa: F401 （供断言引用）
        from app.widgets.cards.global_card_controller import GlobalCardController

        reg = ProviderRegistry()
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
        # 阻断 ensure_loaded 扫真实插件目录（同 test_provider_picker_card.registry）
        reg._warmup_done = True
        reg.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")

        cm = MagicMock()
        monkeypatch.setattr("app.widgets.cards.card_manager.CardManager.get_instance", lambda: cm)

        host = QWidget()  # 卡片 parent 要求真 QWidget（QFrame 类型检查）
        controller = GlobalCardController(host, MagicMock())
        controller._card_manager = cm
        return controller

    def test_picker_view_mounts_wall(self, _qapp, cc):
        """卡片墙视图：内容区挂墙、无保存钮、无面包屑（title 显示）"""
        cc._show_provider_picker_card()
        card = cc._provider_picker_card
        card.show()
        _qapp.processEvents()
        layout = card.content_layout
        assert layout.count() == 1
        assert layout.itemAt(0).widget() is cc._provider_picker_popup
        # isHidden 只看自身显隐标记（isVisible 依赖父链，host 未 show 会误判）
        assert not card.title_label.isHidden()
        assert card._breadcrumb_widget.isHidden()
        assert cc._provider_view == "picker"

    def test_edit_view_swaps_in_form(self, cc):
        """进编辑视图：同一张卡内容区换表单，面包屑两项，保存钮挂上"""
        cc._show_provider_picker_card()
        cc._show_provider_preset_card("DeepSeek")

        assert cc._provider_view == "edit"
        assert cc._provider_origin == "picker"
        layout = cc._provider_picker_card.content_layout
        assert layout.count() == 1
        assert layout.itemAt(0).widget() is cc._provider_edit_popup
        # 面包屑：根 + 当前项
        texts = [
            cc._provider_picker_card._breadcrumb_layout.itemAt(i).widget().text()
            for i in range(cc._provider_picker_card._breadcrumb_layout.count())
            if cc._provider_picker_card._breadcrumb_layout.itemAt(i).widget() is not None
        ]
        assert texts == ["服务商", "›", "添加: DeepSeek"]
        # 同一张卡：不再打开 provider_edit
        from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

        cc._card_manager.show_card.assert_called_with("provider_picker", GLOBAL_WINDOW_ID)

    def test_breadcrumb_back_returns_to_wall(self, cc):
        """添加链：点面包屑根 → 卡内切回卡片墙，不关卡"""
        cc._show_provider_picker_card()
        cc._show_provider_preset_card("DeepSeek")
        cc._on_provider_breadcrumb_root()

        assert cc._provider_view == "picker"
        layout = cc._provider_picker_card.content_layout
        assert layout.itemAt(0).widget() is cc._provider_picker_popup
        # 不关卡：show_card("settings") 不应被调用
        for call in cc._card_manager.show_card.call_args_list:
            assert call.args[0] != "settings", "卡内回退不得打开设置卡"

    def test_settings_origin_back_opens_settings(self, cc, monkeypatch):
        """编辑链：点面包屑根 → 关卡 + 回设置卡服务商页"""
        opened = []
        monkeypatch.setattr(cc, "open_settings", lambda tab=None: opened.append(tab))

        cc._show_provider_picker_card()
        cc._show_provider_edit_card("cfg-1", {"name": "MyProvider", "API_URL": "https://x"})
        assert cc._provider_origin == "settings"

        cc._on_provider_breadcrumb_root()
        assert opened == ["provider"]
        from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

        cc._card_manager.hide_card.assert_called_with("provider_picker", GLOBAL_WINDOW_ID)

    def test_save_closes_card(self, cc, monkeypatch):
        """保存成功 → 关整张卡（保持现状），回设置卡"""
        closed = []
        monkeypatch.setattr(cc, "_close_edit_card", lambda cid: closed.append(cid))
        # 杜绝测试读写用户真实配置（Settings 进程单例跨测试残留 → 假冲突）
        monkeypatch.setattr(type(cc.cfg).llm_saved_providers, "value", {}, raising=False)
        monkeypatch.setattr(cc.cfg, "set", lambda *a, **kw: None)

        cc._show_provider_picker_card()
        cc._show_provider_preset_card("DeepSeek")
        info = {
            "provider_name": "TestProvider",
            "name": "TestProvider",
            "API_URL": "https://api.test.example",
            "API_KEY": "sk-test",
            "认证方式": "bearer",
        }
        cc._on_provider_edit_saved("TestProvider", info, is_new=True)
        assert closed == ["provider_picker"]

    def test_picker_wall_refreshes_on_reopen(self, cc):
        """插件安装（注册表新增）后重开卡片墙 → 内容拾取新服务商"""
        cc._show_provider_picker_card()
        assert "NewProvider" not in cc._provider_picker_popup.provider_names()

        ProviderRegistry.get_instance().register(
            ProviderDef(name="NewProvider", api_url="https://api.new.com"), source="plugin:new"
        )
        cc._show_provider_picker_card()
        assert "NewProvider" in cc._provider_picker_popup.provider_names()

    def test_refresh_provider_wall_picks_up_in_picker_view(self, cc):
        """picker 视图 + 墙已挂载显示 → 广播刷新拾取新服务商"""
        cc._show_provider_picker_card()
        assert cc._provider_view == "picker"
        assert not cc._provider_picker_popup.isHidden(), "mount 后墙应处于显示态"

        ProviderRegistry.get_instance().register(
            ProviderDef(name="NewProvider", api_url="https://api.new.com"), source="plugin:new"
        )
        cc.refresh_provider_wall()
        assert "NewProvider" in cc._provider_picker_popup.provider_names()

    def test_refresh_provider_wall_skipped_in_edit_view(self, cc):
        """编辑视图时广播刷新不动作（墙非当前视图，不打扰表单）"""
        cc._show_provider_picker_card()
        cc._show_provider_preset_card("DeepSeek")
        assert cc._provider_view == "edit"

        ProviderRegistry.get_instance().register(
            ProviderDef(name="NewProvider", api_url="https://api.new.com"), source="plugin:new"
        )
        cc.refresh_provider_wall()
        assert "NewProvider" not in cc._provider_picker_popup.provider_names()

    def test_refresh_provider_wall_safe_before_ensure(self, cc):
        """卡片未懒构建时调用刷新 → 静默无副作用（广播早于首次打开）"""
        assert cc._provider_picker_card is None
        cc.refresh_provider_wall()  # 不抛错即可
