# -*- coding: utf-8 -*-
"""
大模型设置卡片 - 垂直列表布局，高度不够滚动
现已迁移到 SystemCardFrame 基类，获得统一头部布局和固定边框
"""

from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QRect
from PyQt5.QtGui import QFont, QColor
from PyQt5.QtWidgets import (
    QFontComboBox,
)
from loguru import logger
from qfluentwidgets import (
    StrongBodyLabel,
    SwitchSettingCard,
    OptionsSettingCard,
    FluentIcon, SettingCard, PrimaryPushButton, ComboBox, SwitchButton,
)

from app.utils.config import Settings
from app.utils.design_tokens import (
    ButtonStyles,
    ComboBoxStyles,
    FONT_SIZE_OPTIONS,
    THEME_STYLE_OPTIONS,
    Colors,
)
from app.utils.design_tokens import get_ui_font_size, apply_font_size_to_widget
from app.utils.startup_manager import set_auto_start
from app.utils.utils import get_icon, get_unified_font, get_font_family_css
from app.widgets.gateway_setting_card import GatewaySettingCard
from app.widgets.base_settings_card import BaseSettingsCard
from app.widgets.list_setting_card import SkillListSettingCard
from app.widgets.mcp_setting_card import MCPListSettingCard
from app.widgets.provider_setting_card import ProviderListSettingCard
from app.widgets.system_card_frame import SystemCardFrame


class NoWheelFontComboBox(QFontComboBox):
    """禁用滚轮切换的字体下拉框"""

    def wheelEvent(self, event):
        event.ignore()


class NoWheelComboBox(ComboBox):
    def wheelEvent(self, event):
        event.ignore()


class ManualUpdateCard(SettingCard):
    def __init__(self, title, content, parent_widget, parent=None):
        super().__init__(FluentIcon.SYNC, title, content, parent)
        self.parent_widget = parent_widget

        self.updateBtn = PrimaryPushButton("检查更新", self)
        self.updateBtn.setFixedWidth(100)
        self.updateBtn.setStyleSheet(ButtonStyles.primary_action())
        self.updateBtn.clicked.connect(self._on_check_update)
        self.hBoxLayout.addWidget(self.updateBtn, 0, Qt.AlignRight)

    def _on_check_update(self):
        from app.update_checker import UpdateChecker

        self.updateBtn.setText("检查中...")
        self.updateBtn.setEnabled(False)

        checker = UpdateChecker(self.parent_widget)
        checker.finished.connect(self._on_check_finished)
        checker.finished.connect(self._on_check_finished_final)
        checker.error.connect(self._on_error)
        checker.check_update()

    def _on_check_finished(self, latest_release):
        pass

    def _on_check_finished_final(self, latest_release):
        self.updateBtn.setText("检查更新")
        self.updateBtn.setEnabled(True)

    def _on_error(self, msg):
        self.updateBtn.setText("检查更新")
        self.updateBtn.setEnabled(True)
        logger.error(msg)

    def _on_error(self, msg):
        try:
            self.updateBtn.setText("检查更新")
            self.updateBtn.setEnabled(True)
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.error(
                title="检查更新失败",
                content=msg,
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=self.parent_widget,
            ).show()
        except Exception as e:
            print(f"_on_error error: {e}")


class LLMSettingsCard(SystemCardFrame):
    """大模型设置卡片 - 固定边框 + 垂直列表布局"""

    closed = pyqtSignal()
    configChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.set_icon("⚙️")
        self.set_title_text("系统设置")
        self.setFixedHeight(350)

        self.cfg = Settings.get_instance()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._perform_save)

        # 存储各区域分隔标签的位置
        self._section_anchors = {}

        # 设置顶部 Tab 导航
        self.setup_tabs([
            ("llm", "大模型"),
            ("common", "通用设置"),
            ("appearance", "外观样式"),
            ("update", "版本更新"),
        ], default_tab="llm")
        self.tabChanged.connect(self._on_tab_changed)

        self._setup_content()
        
        # 初始化时应用配置中的字体大小和主题样式
        QTimer.singleShot(0, self._refresh_appearance_from_config)

    def _make_sep_label(self, text: str) -> StrongBodyLabel:
        """创建带主题色的分隔标签"""
        Colors.refresh()
        sep_label = StrongBodyLabel(text, self)
        sep_label.setFont(get_unified_font(10, True))
        sep_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; padding: 4px 0;"
            f"{get_font_family_css()} font-weight: bold;"
        )
        return sep_label

    def _setup_content(self):
        content_layout = self.content_layout
        content_layout.setContentsMargins(0, 4, 0, 4)
        content_layout.setSpacing(6)

        # ---- 大模型分隔标签 ----
        self._sep_llm_label = self._make_sep_label("大模型")
        self._section_anchors["llm"] = self._sep_llm_label
        content_layout.addWidget(self._sep_llm_label)

        self.llmProviderCard = ProviderListSettingCard(
            icon=get_icon("大模型"),
            configItem=self.cfg.llm_saved_providers,
            defaultProviderItem=self.cfg.llm_selected_model,
            title="已保存的服务商",
            content="管理已配置的大模型服务商",
            parent=self,
            home=self,
        )
        content_layout.addWidget(self.llmProviderCard)

        self.llmSkillsCard = SkillListSettingCard(
            icon=get_icon("智能体"),
            configItem=self.cfg.llm_enabled_skills,
            title="启用技能",
            content="选择要注入的技能",
            parent=self,
            home=self,
        )
        content_layout.addWidget(self.llmSkillsCard)

        # Hooks 管理
        from app.widgets.hook_setting_card import HookListSettingCard

        hook_manager = getattr(self.parent(), 'backend', None)
        if hook_manager:
            hook_manager = hook_manager.hook_manager

        self.hookListCard = HookListSettingCard(
            icon=get_icon("hooks"),
            title="Hooks 管理",
            content="管理全局 Hooks",
            parent=self,
            home=self,
            hook_manager=hook_manager,
        )
        content_layout.addWidget(self.hookListCard)

        # MCP 服务器管理
        self.mcpListCard = MCPListSettingCard(
            icon=get_icon("MCP"),
            title="MCP 服务器",
            content="管理 MCP Server 连接",
            parent=self,
        )
        content_layout.addWidget(self.mcpListCard)

        # ---- 通用设置分隔标签 ----
        self._sep_common_label = self._make_sep_label("通用设置")
        self._section_anchors["common"] = self._sep_common_label
        content_layout.addWidget(self._sep_common_label)

        # Gateway 通讯平台接入
        self.gatewayCard = GatewaySettingCard(
            icon=get_icon("云通信"),
            title="通讯平台接入",
            content="接入企业微信/钉钉",
            parent=self,
            home=self,
        )
        content_layout.addWidget(self.gatewayCard)

        # 开机自启
        self.autoStartCard = SwitchSettingCard(
            get_icon("开机自动启动"),
            "开机自启",
            "系统启动时自动运行 Drifox",
            self.cfg.auto_start,
            self,
        )
        self.autoStartCard.checkedChanged.connect(self._on_toggled)
        content_layout.addWidget(self.autoStartCard)

        # 智能体完成通知
        self.llmNotifyCard = SwitchSettingCard(
            get_icon("提示"),
            "智能体完成通知",
            "窗口不在前台时发送通知",
            configItem=self.cfg.llm_notify_enabled,
            parent=self,
        )
        content_layout.addWidget(self.llmNotifyCard)

        # 通知提示音
        self.llmSoundCard = OptionsSettingCard(
            self.cfg.llm_notify_sound,
            get_icon("提示"),
            "通知提示音",
            "选择提示音",
            texts=["默认", "短提示音", "无"],
            parent=self,
        )
        content_layout.addWidget(self.llmSoundCard)


        # ---- 外观样式分隔标签 ----
        self._sep_appearance_label = self._make_sep_label("外观样式")
        self._section_anchors["appearance"] = self._sep_appearance_label
        content_layout.addWidget(self._sep_appearance_label)

        # 界面字号、主题风格
        self._setup_appearance_cards()
        content_layout.addWidget(self.uiFontSizeCard)
        content_layout.addWidget(self.uiThemeStyleCard)

        # 全局字体设置
        self._setup_font_card()
        content_layout.addWidget(self.llmFontCard)

        # ---- 版本更新分隔标签 ----
        self._sep_update_label = self._make_sep_label("版本更新")
        self._section_anchors["update"] = self._sep_update_label
        content_layout.addWidget(self._sep_update_label)

        # 自动检查更新
        self.autoUpdateCard = SwitchSettingCard(
            get_icon("提示"),
            "自动检查更新",
            "启动时自动检测新版本",
            configItem=self.cfg.auto_check_update,
            parent=self,
        )
        content_layout.addWidget(self.autoUpdateCard)

        self.manualUpdateCard = ManualUpdateCard(
            "手动检查更新",
            "点击按钮检查是否有新版本",
            self.parent(),
            self.parent(),
        )
        content_layout.addWidget(self.manualUpdateCard)

        content_layout.addStretch(1)

        # 连接信号
        # 注意：只有真正影响外观或模型列表的变更才走 _on_config_changed（触发全量刷新）
        # 技能、通知、提示音等不涉及外观的变更走轻量级保存路径
        self.llmProviderCard.providerChanged.connect(self._on_config_changed)
        self.llmSkillsCard.skillsChanged.connect(self._on_skills_changed)
        self.cfg.llm_notify_enabled.valueChanged.connect(self._on_settings_changed)
        self.llmSoundCard.optionChanged.connect(self._on_settings_changed)
        self.cfg.llm_font_family.valueChanged.connect(self._on_config_changed)
        self.cfg.ui_font_size.valueChanged.connect(self._on_config_changed)
        self.cfg.ui_theme_style.valueChanged.connect(self._on_config_changed)
        self.cfg.llm_api_enabled.valueChanged.connect(self._on_llm_api_enabled_changed)
        self.cfg.llm_api_port.valueChanged.connect(self._on_llm_api_port_changed)

    def _on_tab_changed(self, tab_id: str):
        """Tab 切换时滚动到对应区域"""
        if tab_id in self._section_anchors:
            anchor_widget = self._section_anchors[tab_id]
            # 延迟滚动，等布局稳定后再执行
            QTimer.singleShot(50, lambda: self._scroll_to_widget(anchor_widget))

    def _scroll_to_widget(self, target_widget):
        """滚动到目标控件位置"""
        scroll_area = self.scroll_area
        scroll_bar = scroll_area.verticalScrollBar()
        # 直接设置滚动到目标位置（减去一点边距）
        target_scroll = max(0, target_widget.y() - 10)
        scroll_bar.setValue(target_scroll)

    def _setup_appearance_cards(self):
        class AppearanceComboCard(SettingCard):
            def __init__(self, icon, title, content, cfg, config_item, options, parent=None):
                super().__init__(icon, title, content, parent)
                self.cfg = cfg
                self.config_item = config_item
                self.options = options
                self.value_by_label = {data["label"]: key for key, data in options.items()}
                self.label_by_value = {key: data["label"] for key, data in options.items()}

                self.comboBox = NoWheelComboBox(self)
                self.comboBox.setMaxVisibleItems(6)
                self.comboBox.addItems([data["label"] for data in options.values()])
                self.comboBox.setCurrentText(self.label_by_value.get(config_item.value, next(iter(self.value_by_label))))
                self.comboBox.setMinimumWidth(130)
                self.comboBox.setStyleSheet(ComboBoxStyles.dark_combo())
                self.comboBox.currentTextChanged.connect(self._on_changed)

                self.hBoxLayout.addWidget(self.comboBox)
                self.hBoxLayout.addSpacing(16)

            def _on_changed(self, label):
                value = self.value_by_label.get(label)
                if value:
                    self.cfg.set(self.config_item, value, save=True)
                    parent = self.parent()
                    if parent and hasattr(parent, "_on_config_changed"):
                        parent._on_config_changed()

        self.uiFontSizeCard = AppearanceComboCard(
            get_icon("字体大小"),
            "界面字号",
            "统一调整界面与对话内容字号",
            self.cfg,
            self.cfg.ui_font_size,
            FONT_SIZE_OPTIONS,
            self,
        )
        self.uiThemeStyleCard = AppearanceComboCard(
            get_icon("主题风格"),
            "主题风格",
            "选择一套深色界面卡片配色",
            self.cfg,
            self.cfg.ui_theme_style,
            THEME_STYLE_OPTIONS,
            self,
        )

    def _setup_font_card(self):
        """创建字体设置卡片"""
        from qfluentwidgets import SettingCard

        class FontSettingCard(SettingCard):
            def __init__(self, title, content, cfg, parent=None):
                super().__init__(FluentIcon.FONT, title, content, parent)
                self.cfg = cfg
                self._parent = parent

                self.fontCombo = NoWheelFontComboBox()
                self.fontCombo.setFixedWidth(180)
                self.fontCombo.setSizeAdjustPolicy(QFontComboBox.SizeAdjustPolicy.AdjustToContents)
                self._apply_font_combo_style()
                current_font = cfg.llm_font_family.value
                self.fontCombo.setCurrentFont(QFont(current_font))
                self.fontCombo.currentFontChanged.connect(self._on_font_changed)

                self.hBoxLayout.addWidget(self.fontCombo)
                self.hBoxLayout.addSpacing(16)

            def _apply_font_combo_style(self):
                self.fontCombo.setStyleSheet(ComboBoxStyles.dark_combo().replace("QComboBox", "QFontComboBox"))
                self.fontCombo.view().setStyleSheet(ComboBoxStyles.dark_combo_dropdown())

                view = self.fontCombo.view()
                palette = view.palette()
                palette.setColor(view.backgroundRole(), QColor(42, 42, 46))
                view.setPalette(palette)
                view.setAutoFillBackground(True)
                view.setTextElideMode(Qt.ElideRight)

            def _on_font_changed(self, font):
                self.cfg.set(self.cfg.llm_font_family, font.family(), save=True)
                self.cfg.save()
                if self._parent and hasattr(self._parent, "_on_config_changed"):
                    self._parent._on_config_changed()

        self.llmFontCard = FontSettingCard(
            "全局字体",
            "设置界面显示字体",
            self.cfg,
            self,
        )

    def _setup_port_card(self):
        """创建端口设置卡片"""
        from qfluentwidgets import SettingCard, SpinBox
        from qfluentwidgets import FluentIcon

        class PortSettingCard(SettingCard):
            def __init__(self, title, content, cfg, parent=None):
                super().__init__(FluentIcon.INFO, title, content, parent)
                self.cfg = cfg

                self.spinBox = SpinBox()
                self.spinBox.setFixedWidth(100)
                self.spinBox.setRange(1024, 65535)
                self.spinBox.setValue(cfg.llm_api_port.value)
                self.spinBox.valueChanged.connect(self._on_value_changed)

                self.hBoxLayout.addWidget(self.spinBox)
                self.hBoxLayout.addSpacing(16)

            def _on_value_changed(self, value):
                self.cfg.set(self.cfg.llm_api_port, value, save=True)
                parent = self.parent()
                while parent and not hasattr(parent, "llmApiEnabledCard"):
                    parent = parent.parent()
                if parent and hasattr(parent, "llmApiEnabledCard"):
                    parent.llmApiEnabledCard.setContent(
                        f"http://localhost:{value}/docs"
                    )

        self.llmApiPortCard = PortSettingCard(
            "API 端口",
            "设置 API 服务端口（1024-65535）",
            self.cfg,
            self,
        )

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def _on_skills_changed(self, enabled_skills):
        """技能变更 — 仅保存，不需要刷新外观或模型列表"""
        self._save_timer.start()

    def _on_settings_changed(self, _value=None):
        """非外观类设置变更（通知、提示音等）— 仅保存，不需要刷新外观"""
        self._save_timer.start()

    def _on_config_changed(self):
        """外观/模型相关设置变更 — 需要全量刷新"""
        self.configChanged.emit()
        self._save_timer.start()
        # 立即刷新字体大小和主题样式（不等待保存定时器）
        QTimer.singleShot(0, self._refresh_appearance_from_config)

    def _refresh_appearance_from_config(self):
        """根据当前配置刷新外观样式"""
        # 刷新字体大小
        actual_size = get_ui_font_size()
        apply_font_size_to_widget(self, actual_size)
        
        # 刷新主题样式
        Colors.refresh()
        if hasattr(self, "refresh_style"):
            self.refresh_style()
        
        # 刷新所有子设置卡片的主题样式
        for frame in self.findChildren(SystemCardFrame):
            if hasattr(frame, "refresh_style"):
                frame.refresh_style()
        # 刷新 BaseSettingsCard 子卡片
        for card in self.findChildren(BaseSettingsCard):
            if hasattr(card, "refresh_style"):
                card.refresh_style()

    def _perform_save(self):
        try:
            self.cfg.save_config()
        except Exception as e:
            print(f"保存配置失败: {e}")

    def _on_toggled(self, enabled: bool):
        """开机自启开关切换时：检查平台支持 + 更新注册表"""
        if enabled:
            # 开启前检查平台支持
            import os
            if os.name != "nt":
                self.autoStartCard.switchButton.setChecked(False)
                from qfluentwidgets import InfoBar, InfoBarPosition
                InfoBar.error(
                    title="开机自启",
                    content="当前平台不支持开机自启配置。",
                    position=InfoBarPosition.BOTTOM,
                    duration=3000,
                    parent=self,
                ).show()
                return

        try:
            set_auto_start(enabled)
            # 确保配置持久化到 Settings 文件（.drifox/app.config）
            self.cfg.save()
        except Exception as exc:
            # 失败时回退开关状态和 ConfigItem 值
            self.autoStartCard.switchButton.setChecked(not enabled)
            self.cfg.set(self.cfg.auto_start, not enabled, save=True)
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.error(
                title="开机自启设置失败",
                content=str(exc),
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=self,
            ).show()

    def _on_llm_api_enabled_changed(self, enabled):
        from app.api import (
            stop_llm_api_service,
            is_service_running,
            get_llm_api_service,
        )

        if enabled:
            if not is_service_running():
                service = get_llm_api_service()
                service.port = self.cfg.llm_api_port.value
                service.start(background=True)
        else:
            if is_service_running():
                stop_llm_api_service()
        self._on_settings_changed()

    def _on_llm_api_port_changed(self, port):
        from app.api import (
            stop_llm_api_service,
            is_service_running,
            get_llm_api_service,
        )

        if self.cfg.llm_api_enabled.value and is_service_running():
            stop_llm_api_service()
            service = get_llm_api_service()
            service.port = port
            service.start(background=True)
        if hasattr(self, "llmApiEnabledCard"):
            self.llmApiEnabledCard.setContent(f"http://localhost:{port}/docs")

    def show(self):
        if hasattr(self, 'llmProviderCard'):
            self.llmProviderCard._refresh_items()
        super().show()

    def set_opacity(self, opacity: float):
        """设置透明度（保留接口，暂不实现动态透明度）"""
        pass

