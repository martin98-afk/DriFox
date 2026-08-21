# -*- coding: utf-8 -*-
"""
大模型设置卡片 - 垂直列表布局，高度不够滚动
现已迁移到 SystemCardFrame 基类，获得统一头部布局和固定边框
"""

from loguru import logger
from PyQt5.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QFontComboBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox,
    FluentIcon,
    OptionsSettingCard,
    PrimaryPushButton,
    SettingCard,
    StrongBodyLabel,
    SwitchSettingCard,
)

from app.utils.config import Settings
from app.utils.design_tokens import (
    FONT_SIZE_OPTIONS,
    ButtonStyles,
    Colors,
    ComboBoxStyles,
    apply_font_size_to_widget,
    get_ui_font_size,
    invalidate_font_cache,
    scale_icon_size,
)
from app.utils.startup_manager import set_auto_start
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_font_family_css, get_icon, get_unified_font, invalidate_font_family_css_cache
from app.widgets.cards.settings.base_settings_card import BaseSettingsCard
from app.widgets.cards.settings.gitee_card import GiteeCard
from app.widgets.cards.settings.list_setting_card import SkillListSettingCard
from app.widgets.cards.settings.mcp_setting_card import MCPListSettingCard
from app.widgets.cards.settings.provider_setting_card import ProviderListSettingCard
from app.widgets.cards.settings.system_card_frame import SystemCardFrame


class NoWheelFontComboBox(QFontComboBox):
    """禁用滚轮切换的字体下拉框"""

    def wheelEvent(self, event):
        event.ignore()


class NoWheelComboBox(ComboBox):
    def wheelEvent(self, event):
        event.ignore()


class RefreshableThemeComboBox(ComboBox):
    """主题下拉框 - 热重载信号驱动，自动刷新列表"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._themes_changed = False
        # 注册热重载回调：后端检测到主题文件变更后会触发
        from app.utils.theme_manager import theme_manager

        theme_manager.on_reload(self._mark_themes_changed)

    def destroy(self, destroyWindow=True, destroySubWindows=True):
        from app.utils.theme_manager import theme_manager

        theme_manager.remove_reload_callback(self._mark_themes_changed)
        super().destroy(destroyWindow, destroySubWindows)

    def wheelEvent(self, event):
        event.ignore()

    def _mark_themes_changed(self):
        """热重载回调：标记主题已变更，下次打开时刷新列表"""
        self._themes_changed = True

    def _refresh_items(self):
        """从当前 theme_manager 重建下拉列表项（不重复 reload）

        按当前深浅模式过滤，只显示符合当前模式的主题。
        """
        from app.utils.theme_manager import theme_manager

        # 先找到父 card 以获取配置信息
        card = self.parent()
        if not card or not hasattr(card, "config_item"):
            p = self.parent()
            while p and not hasattr(p, "config_item"):
                p = p.parent()
            card = p

        # 获取所有主题，并按当前深浅模式过滤
        themes = theme_manager.list_themes()
        if card and hasattr(card, "cfg") and hasattr(card.cfg, "ui_light_mode"):
            is_light = card.cfg.ui_light_mode.value
            themes = {tid: name for tid, name in themes.items() if theme_manager.is_light_theme(tid) == is_light}

        new_options = {tid: {"label": name} for tid, name in themes.items()}
        if card and hasattr(card, "config_item"):
            current_key = card.config_item.value
            card.options = new_options
            card.value_by_label = {data["label"]: key for key, data in new_options.items()}
            card.label_by_value = {key: data["label"] for key, data in new_options.items()}
            if current_key not in card.label_by_value:
                current_key = list(new_options.keys())[0]
            self.currentTextChanged.disconnect(card._on_changed)
            self.clear()
            self.addItems([data["label"] for data in new_options.values()])
            self.setCurrentText(card.label_by_value.get(current_key, ""))
            self.currentTextChanged.connect(card._on_changed)
        self._themes_changed = False

    def _toggleComboMenu(self):
        """打开下拉前检查是否需要刷新"""
        try:
            if self._themes_changed:
                self._refresh_items()
        except Exception as e:
            logger.warning(f"[ThemeComboBox] refresh error: {e}")
        super()._toggleComboMenu()


class ManualUpdateCard(SettingCard):
    def __init__(self, title, content, parent_widget, parent=None):
        super().__init__(FluentIcon.SYNC, title, content, parent)
        self.parent_widget = parent_widget

        self.updateBtn = PrimaryPushButton("检查更新", self)
        self.updateBtn.setFixedWidth(100)
        self.updateBtn.setStyleSheet(ButtonStyles.primary_action())
        self.updateBtn.setFocusPolicy(Qt.NoFocus)  # 不参与焦点链，防止禁用时焦点转移导致滚动跳转
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
            logger.error(f"_on_error error: {e}")


class LLMSettingsCard(SystemCardFrame):
    """大模型设置卡片 - 固定边框 + 垂直列表布局"""

    _autostart_toggling = False  # 类级防重入标志
    _last_change_type: str | None = None  # "theme" | "font_family" | "font_size" | None(=全部)
    closed = pyqtSignal()
    configChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.set_icon("⚙️")
        self.set_title_text("系统设置")
        self.setMinimumHeight(250)  # 自适应窗口高度，showEvent 会自动设置 maximumHeight

        self.cfg = Settings.get_instance()

        # 存储各区域分隔标签的位置
        self._section_anchors = {}

        # 设置顶部 Tab 导航
        self.setup_tabs(
            [
                ("llm", "大模型"),
                ("common", "通用设置"),
                ("appearance", "外观样式"),
                ("update", "版本更新"),
                ("plugins", "插件设置"),
            ],
            default_tab="llm",
        )
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
            f"color: {Colors.TEXT_MUTED}; padding: 4px 0;{get_font_family_css()} font-weight: bold;"
        )
        return sep_label

    def _setup_content(self):
        content_layout = self.content_layout
        content_layout.setContentsMargins(0, 4, 0, 4)
        content_layout.setSpacing(6)

        # ---- Gitee 账号绑定（最顶部） ----
        self.giteeCard = GiteeCard(self)
        content_layout.addWidget(self.giteeCard)

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
        from app.widgets.cards.settings.hook_setting_card import HookListSettingCard

        hook_manager = getattr(self.parent(), "backend", None)
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

        # LSP 语言服务器状态
        from app.widgets.cards.settings.lsp_setting_card import LspListSettingCard

        self.lspListCard = LspListSettingCard(
            icon=get_icon("lsp"),
            title="LSP 语言服务器",
            content="代码智能与诊断",
            parent=self,
        )
        content_layout.addWidget(self.lspListCard)

        # ---- 通用设置分隔标签 ----
        self._sep_common_label = self._make_sep_label("通用设置")
        self._section_anchors["common"] = self._sep_common_label
        content_layout.addWidget(self._sep_common_label)

        # 锁屏远程
        self.lockRemoteCard = SwitchSettingCard(
            FluentIcon.SYNC,
            "锁屏远程",
            "保持系统唤醒、屏幕常亮。",
            configItem=self.cfg.lock_screen_remote_enabled,
            parent=self,
        )
        self.lockRemoteCard.checkedChanged.connect(self._on_lock_remote_toggled)
        content_layout.addWidget(self.lockRemoteCard)

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

        # 简洁模式：工具调用/思考块折叠显示
        self.compactToolCard = SwitchSettingCard(
            FluentIcon.MENU,
            "简洁模式",
            "工具与思考归拢到可滚动区域",
            configItem=self.cfg.ui_compact_tool_area,
            parent=self,
        )
        content_layout.addWidget(self.compactToolCard)

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

        # 对话引擎槽位选择（Task 8：UI 先行；激活源过滤消费 TODO 留给下版）
        from app.widgets.cards.settings.engine_slot_card import EngineSlotCard

        self.engineSlotCard = EngineSlotCard(self)
        content_layout.addWidget(self.engineSlotCard)

        # ---- 外观样式分隔标签 ----
        self._sep_appearance_label = self._make_sep_label("外观样式")
        self._section_anchors["appearance"] = self._sep_appearance_label
        content_layout.addWidget(self._sep_appearance_label)

        # 界面字号、浅色模式、主题风格
        self._setup_appearance_cards()
        content_layout.addWidget(self.uiFontSizeCard)
        content_layout.addWidget(self.uiLightModeCard)
        content_layout.addWidget(self.uiThemeStyleCard)

        # 全局字体设置
        self._setup_font_card()
        content_layout.addWidget(self.llmFontCard)

        # 桌宠显示开关
        self.petCard = SwitchSettingCard(
            FluentIcon.HEART,
            "桌宠显示",
            "在主窗口上显示像素小狐桌宠",
            configItem=self.cfg.pet_enabled,
            parent=self,
        )
        content_layout.addWidget(self.petCard)

        # 桌宠大小
        self.petSizeCard = OptionsSettingCard(
            self.cfg.pet_size,
            FluentIcon.ZOOM,
            "桌宠大小",
            "调整像素桌宠的显示尺寸",
            texts=["小 (32px)", "中 (48px)", "大 (64px)"],
            parent=self,
        )
        content_layout.addWidget(self.petSizeCard)

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

        # ---- Phase D: 插件设置分区（初始隐藏，有注册卡片时显示）----
        self._plugin_cards_label = self._make_sep_label("插件设置")
        self._section_anchors["plugins"] = self._plugin_cards_label
        self._plugin_cards_label.setVisible(False)
        content_layout.addWidget(self._plugin_cards_label)
        self._plugin_cards_widget = QWidget(self)
        self._plugin_cards_layout = QVBoxLayout(self._plugin_cards_widget)
        self._plugin_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._plugin_cards_layout.setSpacing(6)
        content_layout.addWidget(self._plugin_cards_widget)
        self._plugin_cards_widget.setVisible(False)
        # 右上角 tab：初始隐藏（rebuild_plugin_cards 按注册卡片显隐）
        try:
            self._tab_buttons["plugins"].setVisible(False)
        except Exception:
            pass

        content_layout.addStretch(1)

        # 连接信号
        # 注意：只有真正影响外观的变更才走 _on_config_changed（触发全量刷新）
        self.cfg.llm_font_family.valueChanged.connect(self._on_config_changed)
        self.cfg.ui_font_size.valueChanged.connect(self._on_config_changed)
        self.cfg.ui_theme_style.valueChanged.connect(self._on_config_changed)
        self.cfg.ui_light_mode.valueChanged.connect(self._on_light_mode_changed)
        self.cfg.llm_api_enabled.valueChanged.connect(self._on_llm_api_enabled_changed)
        self.cfg.llm_api_port.valueChanged.connect(self._on_llm_api_port_changed)

        # 列表形式配置卡片手风琴：展开一个时自动收起其他
        self._list_cards = [
            self.llmProviderCard,
            self.llmSkillsCard,
            self.hookListCard,
            self.mcpListCard,
            self.lspListCard,
        ]
        self._apply_list_accordion()

    def rebuild_plugin_cards(self):
        """重建插件设置分区（Phase D，幂等）

        按 UIPluginRegistry.get_settings_cards() 实例化插件卡片 widget_class；
        无注册卡片时整个分区隐藏（行为零变化）。设置弹窗每次打开时调用，
        保证插件增删/热重载后分区内容最新。
        """
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            cards = UIPluginRegistry.get_instance().get_settings_cards()
        except Exception:
            cards = []
        # 清空旧卡片
        while self._plugin_cards_layout.count():
            item = self._plugin_cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not cards:
            self._plugin_cards_label.setVisible(False)
            self._plugin_cards_widget.setVisible(False)
            try:
                self._tab_buttons["plugins"].setVisible(False)
            except Exception:
                pass
            return
        for info in cards:
            try:
                card = info.widget_class(parent=self._plugin_cards_widget)
                self._plugin_cards_layout.addWidget(card)
                # 动态创建的新卡片不在 _apply_runtime_ui_settings 上一轮 apply 范围
                # 内（首次启动 / 每次重建都未跑 apply），主动触发字号 / 字族应用，
                # 与全局设置弹窗其他卡片一致
                try:
                    from app.utils.design_tokens import apply_font_size_to_widget

                    apply_font_size_to_widget(card, 14)
                except Exception as e:
                    logger.warning(f"[LLMSettingsCard] 插件卡片字号应用失败 {info.card_id}: {e}")
            except Exception as e:
                logger.warning(f"[LLMSettingsCard] 插件设置卡片 {info.card_id} 构建失败：{e}")
        self._plugin_cards_label.setVisible(True)
        self._plugin_cards_widget.setVisible(True)
        try:
            self._tab_buttons["plugins"].setVisible(True)
        except Exception:
            pass

    def _apply_list_accordion(self):
        """为列表形式配置卡片应用手风琴效果

        包装每个 ExpandSettingCard 子类的 setExpand 方法：展开某张卡片时，
        自动收起其他已展开的列表卡片，并把当前卡片的 header 滚到弹窗顶部。
        点击已展开卡片头部仍可正常收起。
        """
        for card in self._list_cards:
            original_set_expand = card.setExpand
            # 通过闭包捕获当前 card 和其他兄弟卡片
            siblings = [c for c in self._list_cards if c is not card]

            def _wrapped_set_expand(is_expand, _orig=original_set_expand, _sibs=siblings, _card=card):
                if is_expand:
                    for sib in _sibs:
                        if sib.isExpand:
                            sib.setExpand(False)
                _orig(is_expand)
                # 展开后等动画/布局稳定，再把卡片内的「焦点 item」滚到弹窗顶部
                # 例如：ProviderListSettingCard 滚到默认 provider；其他卡片 fallback 到第一项
                if is_expand:
                    # qfluentwidgets 展开动画通常 200ms+，等动画结束再算坐标
                    QTimer.singleShot(350, lambda c=_card: self._scroll_focus_item_to_top(c))

            card.setExpand = _wrapped_set_expand

    def _scroll_focus_item_to_top(self, card):
        """把卡片 header 滚到外层 scroll_area 顶部，让卡片标题 + 下方 item 都在弹窗可见区

        ExpandSettingCard.card 是 HeaderSettingCard（含卡片标题行），
        滚到它的顶部后，下方 item list 自然跟在后面同时显示。
        """
        try:
            scroll_area = self.scroll_area
            if scroll_area is None:
                logger.warning("[FocusScroll] scroll_area 为空，提前 return")
                return
            content_widget = scroll_area.widget()
            if content_widget is None:
                return
            # ExpandSettingCard 自带的 header widget（含图标/标题/展开按钮）
            header_widget = getattr(card, "card", None)
            if header_widget is None:
                logger.warning(f"[FocusScroll] {card.__class__.__name__} 没有 .card 属性")
                return
            doc_y = header_widget.mapTo(content_widget, QPoint(0, 0)).y()
            # header 顶部对齐视窗顶部，留 5px 边距
            target = max(0, doc_y - 5)
            scroll_bar = scroll_area.verticalScrollBar()
            old_val = scroll_bar.value()
            scroll_bar.setValue(target)
            logger.info(
                f"[FocusScroll] {card.__class__.__name__} -> header doc_y={doc_y} target={target} "
                f"old={old_val} new={scroll_bar.value()}"
            )
        except Exception as e:
            logger.warning(f"[FocusScroll] 异常: {e}")

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
            def __init__(self, icon, title, content, cfg, config_item, options, parent=None, is_theme_card=False):
                super().__init__(icon, title, content, parent)
                self.cfg = cfg
                self.config_item = config_item
                self.options = options
                self.is_theme_card = is_theme_card
                self._build_lookup_tables()

                if is_theme_card:
                    self.comboBox = RefreshableThemeComboBox(self)
                else:
                    self.comboBox = NoWheelComboBox(self)
                self.comboBox.setMaxVisibleItems(6)
                self.comboBox.addItems([data["label"] for data in options.values()])
                self.comboBox.setCurrentText(
                    self.label_by_value.get(config_item.value, next(iter(self.value_by_label)))
                )
                self.comboBox.setMinimumWidth(130)
                ComboBoxStyles.apply(self.comboBox)
                self.comboBox.currentTextChanged.connect(self._on_changed)

                self.hBoxLayout.addWidget(self.comboBox)
                self.hBoxLayout.addSpacing(16)

            def _build_lookup_tables(self):
                self.value_by_label = {data["label"]: key for key, data in self.options.items()}
                self.label_by_value = {key: data["label"] for key, data in self.options.items()}

            def refresh_style(self):
                """主题变更时刷新下拉框样式"""
                ComboBoxStyles.apply(self.comboBox)

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
        self.uiLightModeCard = SwitchSettingCard(
            get_icon("主题风格"),
            "浅色模式",
            "切换为浅色界面配色",
            self.cfg.ui_light_mode,
            self,
        )
        self.uiThemeStyleCard = AppearanceComboCard(
            get_icon("主题风格"),
            "主题风格",
            "选择界面卡片配色方案",
            self.cfg,
            self.cfg.ui_theme_style,
            self._build_theme_options(),
            self,
            True,  # is_theme_card
        )

    def _build_theme_options(self) -> dict:
        """从 ThemeManager 动态构建主题选项，按当前深浅模式过滤并加标识"""
        from app.utils.config import update_theme_options

        update_theme_options()
        themes = theme_manager.list_themes()
        is_light = self.cfg.ui_light_mode.value

        result = {}
        for tid, name in themes.items():
            theme_light = theme_manager.is_light_theme(tid)
            # 按当前模式过滤
            if is_light != theme_light:
                continue
            label = name
            result[tid] = {"label": label}
        return result

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

            def refresh_style(self):
                """主题变更时刷新字体下拉框样式"""
                self._apply_font_combo_style()

            def _on_font_changed(self, font):
                self.cfg.set(self.cfg.llm_font_family, font.family(), save=True)
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
        from qfluentwidgets import FluentIcon, SettingCard, SpinBox

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
                    parent.llmApiEnabledCard.setContent(f"http://localhost:{value}/docs")

        self.llmApiPortCard = PortSettingCard(
            "API 端口",
            "设置 API 服务端口（1024-65535）",
            self.cfg,
            self,
        )

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def _on_light_mode_changed(self, is_light: bool):
        """浅色模式开关切换 — 自动选择对应模式的主题"""
        if is_light:
            target_theme = "lumia"
        else:
            target_theme = "fallout"
        # 避免循环触发：只有真正需要切换时才改
        if self.cfg.ui_theme_style.value != target_theme:
            self.cfg.set(self.cfg.ui_theme_style, target_theme, save=True)
        # 重建主题下拉列表（按模式过滤）
        if hasattr(self, "uiThemeStyleCard") and hasattr(self.uiThemeStyleCard, "comboBox"):
            card = self.uiThemeStyleCard
            card.options = self._build_theme_options()
            card._build_lookup_tables()
            combo = card.comboBox
            combo.currentTextChanged.disconnect(card._on_changed)
            combo.clear()
            combo.addItems([data["label"] for data in card.options.values()])
            if target_theme in card.label_by_value:
                combo.setCurrentText(card.label_by_value[target_theme])
            elif card.options:
                first_tid = next(iter(card.options))
                combo.setCurrentText(card.options[first_tid]["label"])
                self.cfg.set(self.cfg.ui_theme_style, first_tid, save=True)
            combo.currentTextChanged.connect(card._on_changed)
        # 触发全量刷新
        self._on_config_changed()

    def _on_config_changed(self):
        """外观/模型相关设置变更 — 需要全量刷新"""
        # 失效字体/字号缓存，让后续渲染读取新配置
        invalidate_font_cache()
        invalidate_font_family_css_cache()

        # 检测变更类型，传递给主窗口按需刷新
        sender = self.sender()
        if sender is self.cfg.ui_theme_style:
            LLMSettingsCard._last_change_type = "theme"
        elif sender is self.cfg.ui_light_mode:
            # 浅色模式开关切换 → 实质是换主题
            LLMSettingsCard._last_change_type = "theme"
        elif sender is self.cfg.llm_font_family:
            LLMSettingsCard._last_change_type = "font_family"
        elif sender is self.cfg.ui_font_size:
            LLMSettingsCard._last_change_type = "font_size"
        else:
            LLMSettingsCard._last_change_type = None  # 未知类型，全量刷新

        self.configChanged.emit()
        # 所有配置控件在变更时已即时保存，这里只负责刷新运行时外观

    def _refresh_appearance_from_config(self):
        """根据当前配置刷新外观样式

        Colors.refresh() 由 self.refresh_style() 内部第一行调用，
        此处不重复调用。
        """
        # 刷新字体大小
        actual_size = get_ui_font_size()
        apply_font_size_to_widget(self, actual_size)

        # 浅/深色切换 → 清除浅色检测缓存（图标由 QIconEngine 自动适配）
        from app.utils.theme_manager import theme_manager

        theme_manager.on_theme_changed()

        if hasattr(self, "refresh_style"):
            self.refresh_style()

        # ── 合并刷新：一次 findChildren(SystemCardFrame) 覆盖所有 SystemCardFrame 子类 ──
        # BaseSettingsCard 继承自 SystemCardFrame，已在遍历中，无需第二遍 findChildren
        for frame in self.findChildren(SystemCardFrame):
            if hasattr(frame, "refresh_style"):
                frame.refresh_style()
        # AppearanceComboCard / FontSettingCard（SettingCard 子类，不在以上遍历范围）
        for card_name in ("uiFontSizeCard", "uiLightModeCard", "uiThemeStyleCard", "llmFontCard"):
            card = getattr(self, card_name, None)
            if card is not None and hasattr(card, "refresh_style"):
                card.refresh_style()
        # 手风琴类卡片（ExpandSettingCard 子类，不在以上遍历范围）
        for card_name in ("llmSkillsCard", "llmProviderCard", "mcpListCard", "lspListCard"):
            card = getattr(self, card_name, None)
            if card is not None and hasattr(card, "refresh_style"):
                card.refresh_style()
        # ── SettingCard 图标大小（单次 findChildren） ──
        icon_sz = scale_icon_size(16)
        for card in self.findChildren(SettingCard):
            card.setIconSize(icon_sz, icon_sz)
        # ── 分隔标签（Colors 已在上游 refresh_style 链中刷新） ──
        self._refresh_sep_labels()

    def _refresh_sep_labels(self):
        """刷新所有分隔标签的样式

        不额外调用 Colors.refresh()——上游 _refresh_appearance_from_config / refresh_style
        链已确保 Colors 为最新。分隔标签单纯使用 Colors 当前值设置样式。
        """
        sep_labels = [
            getattr(self, "_sep_llm_label", None),
            getattr(self, "_sep_common_label", None),
            getattr(self, "_sep_appearance_label", None),
            getattr(self, "_sep_update_label", None),
        ]
        text_muted = Colors.TEXT_MUTED
        for label in sep_labels:
            if label is not None:
                label.setStyleSheet(f"color: {text_muted}; padding: 4px 0;{get_font_family_css()} font-weight: bold;")

    def refresh_theme_options(self):
        """热更新后刷新主题下拉列表（外部由 _on_plugin_hot_reload 调用）"""
        if hasattr(self, "uiThemeStyleCard") and hasattr(self.uiThemeStyleCard, "comboBox"):
            try:
                combo = self.uiThemeStyleCard.comboBox
                if hasattr(combo, "_refresh_items"):
                    combo._refresh_items()
                    logger.debug("[ThemeComboBox] 主题下拉已主动刷新")
            except Exception as e:
                logger.warning(f"[ThemeComboBox] 主动刷新失败: {e}")

    def _on_toggled(self, enabled: bool):
        """开机自启开关切换时：检查平台支持 + 更新注册表"""
        # 防重入：防止信号递归/连锁导致多次写入
        if LLMSettingsCard._autostart_toggling:
            logger.info(f"[AutoStart] 防重入拦截: enabled={enabled}")
            return
        LLMSettingsCard._autostart_toggling = True
        try:
            if enabled:
                import os

                if os.name != "nt":
                    self.autoStartCard.switchButton.setChecked(False)
                    from qfluentwidgets import InfoBar, InfoBarPosition
                    from app.widgets.tab_manager_window import TabManagerWindow

                    # InfoBar 统一挂到 tab 管理器顶层窗口（未就绪时兜底卡片所在窗口）
                    bar_parent = TabManagerWindow.get_instance() or self.window()
                    InfoBar.error(
                        title="开机自启",
                        content="当前平台不支持开机自启配置。",
                        position=InfoBarPosition.BOTTOM,
                        duration=3000,
                        parent=bar_parent,
                    ).show()
                    return

            # 1. 先写入注册表（独立 try，不相互污染异常处理）
            try:
                set_auto_start(enabled)
            except Exception as exc:
                # 注册表写入失败 → 回退 UI 和配置
                self.autoStartCard.switchButton.setChecked(not enabled)
                self.cfg.set(self.cfg.auto_start, not enabled, save=True)
                from qfluentwidgets import InfoBar, InfoBarPosition
                from app.widgets.tab_manager_window import TabManagerWindow

                # InfoBar 统一挂到 tab 管理器顶层窗口（未就绪时兜底卡片所在窗口）
                bar_parent = TabManagerWindow.get_instance() or self.window()
                InfoBar.error(
                    title="开机自启设置失败",
                    content=str(exc),
                    position=InfoBarPosition.BOTTOM,
                    duration=3000,
                    parent=bar_parent,
                ).show()
                return

            # SwitchSettingCard 已通过 qconfig.set(save=True) 持久化配置
        finally:
            LLMSettingsCard._autostart_toggling = False

    def _on_llm_api_enabled_changed(self, enabled):
        from app.gateway import (
            get_llm_api_service,
            is_service_running,
            stop_llm_api_service,
        )

        if enabled:
            if not is_service_running():
                service = get_llm_api_service()
                service.port = self.cfg.llm_api_port.value
                service.start(background=True)
        else:
            if is_service_running():
                stop_llm_api_service()

    def _on_llm_api_port_changed(self, port):
        from app.gateway import (
            get_llm_api_service,
            is_service_running,
            stop_llm_api_service,
        )

        if self.cfg.llm_api_enabled.value and is_service_running():
            stop_llm_api_service()
            service = get_llm_api_service()
            service.port = port
            service.start(background=True)
        if hasattr(self, "llmApiEnabledCard"):
            self.llmApiEnabledCard.setContent(f"http://localhost:{port}/docs")

    def _on_lock_remote_toggled(self, enabled: bool):
        """锁屏远程开关：开启时保持系统/屏幕唤醒并锁屏，关闭时恢复休眠策略"""
        from qfluentwidgets import InfoBar, InfoBarPosition
        from app.widgets.tab_manager_window import TabManagerWindow

        from app.core.system.lock_screen_remote import get_lock_screen_remote_manager

        # InfoBar 统一挂到 tab 管理器顶层窗口（未就绪时兜底卡片所在窗口）
        bar_parent = TabManagerWindow.get_instance() or self.window()
        mgr = get_lock_screen_remote_manager()
        if enabled:
            mgr.enable(lock_now=False, keep_display_on=True)
            InfoBar.success(
                title="锁屏远程",
                content="已开启：系统保持唤醒，屏幕常亮。",
                position=InfoBarPosition.BOTTOM,
                duration=2500,
                parent=bar_parent,
            ).show()
        else:
            mgr.disable()
            InfoBar.info(
                title="锁屏远程",
                content="已关闭，恢复系统正常休眠策略。",
                position=InfoBarPosition.BOTTOM,
                duration=2500,
                parent=bar_parent,
            ).show()

    def showEvent(self, event):
        if hasattr(self, "llmProviderCard"):
            self.llmProviderCard._refresh_items()
        super().showEvent(event)

    def set_opacity(self, opacity: float):
        """设置透明度（保留接口，暂不实现动态透明度）"""
        pass
