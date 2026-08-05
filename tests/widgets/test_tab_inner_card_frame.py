# -*- coding: utf-8 -*-
"""TabPanel 内部嵌套圆角矩形容器测试

验证 _wrap_section 的行为：
- wrap frame 跟随子 widget 可见性
- 嵌套样式 token 正确应用
- layout spacing 改为 6px
"""

from unittest.mock import patch

import pytest
from PyQt5.QtWidgets import QApplication

from app.utils.design_tokens import InnerCardStyles
from app.widgets.tab_panel import TabPanel


class TestTabPanelInnerCard:
    @pytest.fixture(autouse=True)
    def _setup(self, qtbot):
        with patch(
            "app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"
        ):
            self.panel = TabPanel()
        qtbot.addWidget(self.panel)

    def test_brand_section_visible_by_default(self):
        """品牌区默认应被 wrap（无显式 hide）"""
        # offscreen 测试环境下 widget.isVisible() 可能延迟；
        # 但 wrap 类型/objectName 是确定可验证的。
        brand_card = self.panel._brand_widget.parent()
        assert brand_card is not None
        assert brand_card.objectName() == "innerCard"
        # brand_section 初始未显式 hide（对比 _system_plugin_section 显式 hidden）
        assert self.panel._brand_widget.isHidden() is False

    def test_system_plugin_section_default_hidden(self):
        """系统插件默认隐藏，wrap frame 也隐藏"""
        # 显式 setVisible(False) 在 _setup_ui 里设置过
        assert self.panel._system_plugin_section.isVisible() is False
        wrap = self.panel._system_plugin_section.parent()
        assert wrap.isVisible() is False

    def test_wrap_section_tracks_child_visibility(self):
        """改变 child widget 的可见性，wrap frame 跟随变化"""
        self.panel.show()
        section = self.panel._system_plugin_section
        wrap = section.parent()
        # 初始：hidden（_setup_ui 调用了 setVisible(False)）
        assert wrap.isVisible() is False
        # 显示 section → 触发 Show event → wrap.show()
        section.setVisible(True)
        # Note: setVisible 在 Qt 中是异步事件，需要 processEvents
        QApplication.processEvents()
        assert wrap.isVisible() is True
        # 隐藏 section → 触发 Hide event → wrap.hide()
        section.setVisible(False)
        QApplication.processEvents()
        assert wrap.isVisible() is False

    def test_wrap_section_style_applied(self):
        """嵌套 frame 应用了 InnerCardStyles.frame() 的边框样式"""
        # 至少包含 BORDER 颜色和 border-radius
        wrap = self.panel._brand_widget.parent()
        stylesheet = wrap.styleSheet()
        assert "border-radius" in stylesheet
        assert "8px" in stylesheet

    def test_main_layout_spacing_is_6(self):
        """主 layout spacing 改为 6px（嵌套圆角矩形之间间距）"""
        assert self.panel.layout().spacing() == 6

    def test_no_legacy_separator_attributes(self):
        """HLine 分隔符属性已删除"""
        for attr in [
            "_brand_separator",
            "_plugin_separator_1",
            "_plugin_separator_2",
        ]:
            assert not hasattr(self.panel, attr), f"残留旧属性 {attr}"

    def test_inner_card_styles_frame(self):
        """InnerCardStyles.frame() 输出符合预期的样式 token"""
        qss = InnerCardStyles.frame()
        assert "border-radius" in qss
        assert "8px" in qss
        # 不画实色背景（透明，让外层 tabFrame 透出）
        assert "transparent" in qss
