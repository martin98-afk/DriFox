# -*- coding: utf-8 -*-
"""
大模型设置卡片 - 垂直列表布局，高度不够滚动
现已迁移到 SystemCardFrame 基类，获得统一头部布局和固定边框
"""

from loguru import logger
from PyQt5.QtCore import QPointF, QRectF, QPoint, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import (
    QFontComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ExpandSettingCard,
    FluentIcon,
    OptionsSettingCard,
    PrimaryPushButton,
    SettingCard,
    SwitchSettingCard,
)

from app.utils.config import Settings
from app.utils.design_tokens import (
    FONT_SIZE_OPTIONS,
    _DEFAULT_FONT_SIZE_KEY,
    _LEGACY_FONT_SIZE_KEYS,
    ButtonStyles,
    Colors,
    ComboBoxStyles,
    apply_font_size_to_widget,
    font_size_css,
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


def _font_step_text(key: str) -> str:
    """字号档位显示文本："-5".."0".."+10"（正数带 + 前缀）"""
    d = int(key)
    return f"+{d}" if d > 0 else str(d)


class _FontStepTrack(QWidget):
    """节点+轨道式字号刻度条

    一条轨道线贯穿 16 个节点（-5..+10），节点下方刻度标签；点击节点直达
    对应档位。当前档位节点 accent 实心 + 外环，0 为基准刻度（标签加粗 +
    节点空心大圆）。颜色/字号在 paintEvent 动态读取主题令牌，主题或字号
    刷新时调用 update() 即可重绘。
    """

    stepClicked = pyqtSignal(str)

    _NODE_Y = 14  # 节点中心线 y
    _PAD = 18  # 首末节点距两侧边距
    _HOVER_R = 4.5  # hover 节点半径
    _NORMAL_R = 3.0  # 普通节点半径
    _ZERO_R = 4.0  # 基准(0)节点半径
    _CUR_INNER_R = 4.0  # 当前节点实心半径
    _CUR_OUTER_R = 7.0  # 当前节点外环半径

    def __init__(self, steps, parent=None):
        super().__init__(parent)
        self._steps = list(steps)
        self._current: str | None = None
        self._hover_idx = -1
        self._label_h = 14  # 刻度标签高度（随字号自适应）
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._recompute_metrics()

    def _recompute_metrics(self) -> None:
        """按当前档位字号重算标签行高与最小高度（防大字号标签被裁切）"""
        from PyQt5.QtGui import QFontMetrics

        fm = QFontMetrics(get_unified_font(9))
        self._label_h = max(14, fm.height() + 2)
        self.setMinimumHeight(self._NODE_Y + self._label_h + 6)
        self.updateGeometry()
        self.update()

    def set_current(self, key: str | None) -> None:
        self._current = key
        self.update()

    # ── 几何映射 ──

    def _node_x(self, idx: int) -> float:
        n = len(self._steps)
        if n <= 1:
            return self.width() / 2
        return self._PAD + (self.width() - 2 * self._PAD) * idx / (n - 1)

    def _index_at(self, x: int) -> int:
        n = len(self._steps)
        span = self.width() - 2 * self._PAD
        if n <= 1 or span <= 0:
            return 0
        frac = (x - self._PAD) / span
        return max(0, min(n - 1, int(round(frac * (n - 1)))))

    # ── 交互 ──

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._steps:
            self.stepClicked.emit(self._steps[self._index_at(event.x())])
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        idx = self._index_at(event.x())
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        if self._hover_idx != -1:
            self._hover_idx = -1
            self.update()
        super().leaveEvent(event)

    # ── 绘制 ──

    def paintEvent(self, event) -> None:
        if not self._steps:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(self._steps)
        node_y = float(self._NODE_Y)
        x0, x1 = self._node_x(0), self._node_x(n - 1)
        cur_idx = self._steps.index(self._current) if self._current in self._steps else -1

        # 轨道底线
        p.setPen(QPen(QColor(Colors.BORDER), 2))
        p.drawLine(int(x0), int(node_y), int(x1), int(node_y))
        # 已选段（起点 → 当前节点）accent
        if cur_idx > 0:
            p.setPen(QPen(QColor(Colors.TEXT_ACCENT), 2))
            p.drawLine(int(x0), int(node_y), int(self._node_x(cur_idx)), int(node_y))

        # 节点 + 刻度标签
        label_font = get_unified_font(9)
        bold_font = get_unified_font(9, True)
        for i, key in enumerate(self._steps):
            x = self._node_x(i)
            is_cur = i == cur_idx
            is_hover = i == self._hover_idx
            is_zero = key == "0"

            # 节点
            if is_cur:
                p.setPen(QPen(QColor(Colors.TEXT_ACCENT), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(x, node_y), self._CUR_OUTER_R, self._CUR_OUTER_R)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(QColor(Colors.TEXT_ACCENT)))
                p.drawEllipse(QPointF(x, node_y), self._CUR_INNER_R, self._CUR_INNER_R)
            elif is_zero:
                p.setPen(QPen(QColor(Colors.TEXT_ACCENT), 1.5))
                p.setBrush(QBrush(QColor(Colors.BUTTON_TEXT_ON_ACCENT)))
                p.drawEllipse(QPointF(x, node_y), self._ZERO_R, self._ZERO_R)
            else:
                color = Colors.TEXT_ACCENT if is_hover else Colors.BORDER
                r = self._HOVER_R if is_hover else self._NORMAL_R
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(QColor(color)))
                p.drawEllipse(QPointF(x, node_y), r, r)

            # 刻度标签（当前 accent 加粗 / hover 主色 / 其余次级；0 基准加粗）
            if is_cur or is_zero:
                p.setFont(bold_font)
                p.setPen(QColor(Colors.TEXT_ACCENT if is_cur else Colors.TEXT_PRIMARY))
            else:
                p.setFont(label_font)
                p.setPen(QColor(Colors.TEXT_PRIMARY if is_hover else Colors.TEXT_SECONDARY))
            p.drawText(
                QRectF(x - 24, node_y + 8, 48, self._label_h),
                Qt.AlignHCenter | Qt.AlignTop,
                _font_step_text(key),
            )


class FontSizeStepperCard(ExpandSettingCard):
    """界面字号刻度条卡 — 折叠展开式，展开区为节点+轨道刻度条（-5px..+10px）

    点击节点直达对应档位；当前档位 accent 高亮、0 为基准刻度。
    header 右侧实时显示当前档位（如 "+2 px"）。
    """

    def __init__(self, icon, title, content, cfg, parent=None):
        super().__init__(icon, title, content, parent)
        self.cfg = cfg

        # header 右侧当前档位值
        self._value_label = QLabel(self)
        self._value_label.setObjectName("titleLabel")
        self.addWidget(self._value_label)

        # 展开区：节点轨道刻度条 + 说明
        self.viewLayout.setContentsMargins(48, 12, 24, 8)
        self.viewLayout.setSpacing(4)
        self._track = _FontStepTrack(list(FONT_SIZE_OPTIONS.keys()), self.view)
        self._track.stepClicked.connect(self._on_step_clicked)
        self.viewLayout.addWidget(self._track)

        self._hint_label = QLabel("点击节点直达档位 · 0 为基准（14px）", self.view)
        self.viewLayout.addWidget(self._hint_label)

        self.cfg.ui_font_size.valueChanged.connect(self._sync_current)
        self._sync_current()

    def _on_step_clicked(self, key: str) -> None:
        if key != self.cfg.ui_font_size.value:
            self.cfg.set(self.cfg.ui_font_size, key, save=True)

    def _sync_current(self, *_args) -> None:
        """同步节点高亮与 header 当前值（配置变更信号驱动；旧档位键兜底映射）"""
        key = self.cfg.ui_font_size.value
        key = _LEGACY_FONT_SIZE_KEYS.get(key, key)
        if key not in FONT_SIZE_OPTIONS:
            key = _DEFAULT_FONT_SIZE_KEY
        self._track.set_current(key)
        self._value_label.setText(f"{_font_step_text(key)} px")

    def refresh_style(self) -> None:
        """主题/字号变更后重绘刻度条与文字样式（颜色/字号在 paint 动态读取）"""
        self._value_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(12)}")
        self._hint_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)}"
        )
        # 字号变化 → 标签行高/最小高度重算（防大字号标签被裁切）+ 重绘
        self._track._recompute_metrics()


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

        # 左侧导航 + 右侧分页：分区归属见 _setup_content
        self._current_tab = "llm"
        self._nav_frame = None  # _build_side_nav 中创建

        self._setup_content()

        # 初始化时应用配置中的字体大小和主题样式
        QTimer.singleShot(0, self._refresh_appearance_from_config)

    def _setup_content(self):
        content_layout = self.content_layout
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # ── 主体：左侧导航 + 右侧分页 ──
        tabs = [
            ("llm", "大模型"),
            ("common", "通用设置"),
            ("appearance", "外观样式"),
            ("update", "版本更新"),
            ("plugins", "插件设置"),
        ]
        body = QWidget(self)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)
        body_layout.addWidget(self._build_side_nav(tabs))

        self._pages_stack = QStackedWidget(self)
        self._page_scrolls = {}
        self._page_layouts = {}
        for tab_id, _name in tabs:
            page, page_layout = self._make_page()
            self._page_scrolls[tab_id] = page
            self._page_layouts[tab_id] = page_layout
            self._pages_stack.addWidget(page)
        body_layout.addWidget(self._pages_stack, 1)
        content_layout.addWidget(body)
        self._update_nav_styles()

        # ════ 大模型页 ════
        llm_layout = self._page_layouts["llm"]

        # Gitee 账号绑定（保持原默认页顶部位置）
        self.giteeCard = GiteeCard(self)
        llm_layout.addWidget(self.giteeCard)

        self.llmProviderCard = ProviderListSettingCard(
            icon=get_icon("大模型"),
            configItem=self.cfg.llm_saved_providers,
            defaultProviderItem=self.cfg.llm_selected_model,
            title="已保存的服务商",
            content="管理已配置的大模型服务商",
            parent=self,
            home=self,
        )
        llm_layout.addWidget(self.llmProviderCard)

        self.llmSkillsCard = SkillListSettingCard(
            icon=get_icon("智能体"),
            configItem=self.cfg.llm_enabled_skills,
            title="启用技能",
            content="选择要注入的技能",
            parent=self,
            home=self,
        )
        llm_layout.addWidget(self.llmSkillsCard)

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
        llm_layout.addWidget(self.hookListCard)

        # MCP 服务器管理
        self.mcpListCard = MCPListSettingCard(
            icon=get_icon("MCP"),
            title="MCP 服务器",
            content="管理 MCP Server 连接",
            parent=self,
        )
        llm_layout.addWidget(self.mcpListCard)

        # LSP 语言服务器状态
        from app.widgets.cards.settings.lsp_setting_card import LspListSettingCard

        self.lspListCard = LspListSettingCard(
            icon=get_icon("lsp"),
            title="LSP 语言服务器",
            content="代码智能与诊断",
            parent=self,
        )
        llm_layout.addWidget(self.lspListCard)
        llm_layout.addStretch(1)

        # ════ 通用设置页 ════
        common_layout = self._page_layouts["common"]

        # 锁屏远程
        self.lockRemoteCard = SwitchSettingCard(
            FluentIcon.SYNC,
            "锁屏远程",
            "保持系统唤醒、屏幕常亮。",
            configItem=self.cfg.lock_screen_remote_enabled,
            parent=self,
        )
        self.lockRemoteCard.checkedChanged.connect(self._on_lock_remote_toggled)
        common_layout.addWidget(self.lockRemoteCard)

        # 开机自启
        self.autoStartCard = SwitchSettingCard(
            get_icon("开机自动启动"),
            "开机自启",
            "系统启动时自动运行 Drifox",
            self.cfg.auto_start,
            self,
        )
        self.autoStartCard.checkedChanged.connect(self._on_toggled)
        common_layout.addWidget(self.autoStartCard)

        # 简洁模式：工具调用/思考块折叠显示
        self.compactToolCard = SwitchSettingCard(
            FluentIcon.MENU,
            "简洁模式",
            "工具与思考归拢到可滚动区域",
            configItem=self.cfg.ui_compact_tool_area,
            parent=self,
        )
        common_layout.addWidget(self.compactToolCard)

        # 智能体完成通知
        self.llmNotifyCard = SwitchSettingCard(
            get_icon("提示"),
            "智能体完成通知",
            "窗口不在前台时发送通知",
            configItem=self.cfg.llm_notify_enabled,
            parent=self,
        )
        common_layout.addWidget(self.llmNotifyCard)

        # 通知提示音
        self.llmSoundCard = OptionsSettingCard(
            self.cfg.llm_notify_sound,
            get_icon("提示"),
            "通知提示音",
            "选择提示音",
            texts=["默认", "短提示音", "无"],
            parent=self,
        )
        common_layout.addWidget(self.llmSoundCard)
        common_layout.addStretch(1)

        # ════ 外观样式页 ════
        appearance_layout = self._page_layouts["appearance"]

        # 界面字号、浅色模式、主题风格
        self._setup_appearance_cards()
        appearance_layout.addWidget(self.uiFontSizeCard)
        appearance_layout.addWidget(self.uiLightModeCard)
        appearance_layout.addWidget(self.uiThemeStyleCard)

        # 全局字体设置
        self._setup_font_card()
        appearance_layout.addWidget(self.llmFontCard)

        # 桌宠显示开关
        self.petCard = SwitchSettingCard(
            FluentIcon.HEART,
            "桌宠显示",
            "在主窗口上显示像素小狐桌宠",
            configItem=self.cfg.pet_enabled,
            parent=self,
        )
        appearance_layout.addWidget(self.petCard)

        # 桌宠大小
        self.petSizeCard = OptionsSettingCard(
            self.cfg.pet_size,
            FluentIcon.ZOOM,
            "桌宠大小",
            "调整像素桌宠的显示尺寸",
            texts=["小 (32px)", "中 (48px)", "大 (64px)"],
            parent=self,
        )
        appearance_layout.addWidget(self.petSizeCard)
        appearance_layout.addStretch(1)

        # ════ 版本更新页 ════
        update_layout = self._page_layouts["update"]

        # 自动检查更新
        self.autoUpdateCard = SwitchSettingCard(
            get_icon("提示"),
            "自动检查更新",
            "启动时自动检测新版本",
            configItem=self.cfg.auto_check_update,
            parent=self,
        )
        update_layout.addWidget(self.autoUpdateCard)

        self.manualUpdateCard = ManualUpdateCard(
            "手动检查更新",
            "点击按钮检查是否有新版本",
            self.parent(),
            self.parent(),
        )
        update_layout.addWidget(self.manualUpdateCard)
        update_layout.addStretch(1)

        # ════ 插件设置页（初始隐藏，有注册卡片时显示）════
        self._plugin_cards_widget = QWidget(self)
        self._plugin_cards_layout = QVBoxLayout(self._plugin_cards_widget)
        self._plugin_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._plugin_cards_layout.setSpacing(6)
        self._page_layouts["plugins"].addWidget(self._plugin_cards_widget)
        self._page_layouts["plugins"].addStretch(1)
        self._plugin_cards_widget.setVisible(False)
        # 左侧导航：初始隐藏（rebuild_plugin_cards 按注册卡片显隐）
        try:
            self._nav_buttons["plugins"].setVisible(False)
        except Exception:
            pass

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
            self._plugin_cards_widget.setVisible(False)
            try:
                self._nav_buttons["plugins"].setVisible(False)
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
        self._plugin_cards_widget.setVisible(True)
        try:
            self._nav_buttons["plugins"].setVisible(True)
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
        """把卡片 header 滚到所在分页滚动区顶部，让卡片标题 + 下方 item 都在可见区

        分页改造后每页有独立 QScrollArea：优先向上找卡片祖先滚动区，
        找不到时回退基类 scroll_area。
        ExpandSettingCard.card 是 HeaderSettingCard（含卡片标题行），
        滚到它的顶部后，下方 item list 自然跟在后面同时显示。
        """
        try:
            scroll_area = self._ancestor_scroll_area(card) or self.scroll_area
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

    # ── 左侧导航 + 分页 ──────────────────────────────

    def _build_side_nav(self, tabs: list) -> QFrame:
        """构建左侧导航面板（垂直 tab 按钮列表）"""
        self._nav_buttons = {}
        nav = QFrame(self)
        nav.setObjectName("settingsSideNav")
        nav.setFixedWidth(148)
        nav.setStyleSheet(self._nav_frame_style())

        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(4, 8, 6, 8)
        nav_layout.setSpacing(2)
        for tab_id, tab_name in tabs:
            btn = QLabel(tab_name, nav)
            btn.setCursor(Qt.PointingHandCursor)
            btn.mousePressEvent = lambda e, tid=tab_id: self._set_active_page(tid)
            nav_layout.addWidget(btn)
            self._nav_buttons[tab_id] = btn
        nav_layout.addStretch(1)
        self._nav_frame = nav
        return nav

    def _make_page(self) -> tuple:
        """创建单个分页：独立 QScrollArea + 垂直内容布局"""
        page = QScrollArea(self)
        page.setWidgetResizable(True)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        page.setStyleSheet(SystemCardFrame._scroll_style())

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(6)
        page.setWidget(inner)
        return page, layout

    def _set_active_page(self, tab_id: str):
        """切换左侧导航到指定分页"""
        if tab_id not in self._nav_buttons or self._current_tab == tab_id:
            return
        self._current_tab = tab_id
        self._pages_stack.setCurrentWidget(self._page_scrolls[tab_id])
        self._update_nav_styles()
        self.tabChanged.emit(tab_id)

    def _update_nav_styles(self):
        for tab_id, btn in self._nav_buttons.items():
            btn.setStyleSheet(self._nav_btn_style(tab_id == self._current_tab))

    def _nav_frame_style(self) -> str:
        return f"""
            QFrame#settingsSideNav {{
                background: transparent;
                border-right: 1px solid {Colors.BORDER};
            }}
        """

    @staticmethod
    def _nav_btn_style(active: bool) -> str:
        if active:
            return f"""
                QLabel {{
                    color: {Colors.TEXT_PRIMARY};
                    {font_size_css(12)}
                    font-weight: bold;
                    padding: 8px 10px;
                    border-radius: 6px;
                    border-left: 3px solid {Colors.TEXT_ACCENT};
                    background-color: {Colors.TAB_ACTIVE_BG};
                    {get_font_family_css()}
                }}
            """
        return f"""
            QLabel {{
                color: {Colors.TEXT_SECONDARY};
                {font_size_css(12)}
                padding: 8px 10px;
                border-radius: 6px;
                border-left: 3px solid transparent;
                {get_font_family_css()}
            }}
            QLabel:hover {{
                color: {Colors.TEXT_PRIMARY};
                background-color: {Colors.TAB_HOVER_BG};
            }}
        """

    @staticmethod
    def _ancestor_scroll_area(widget):
        """向上查找最近的祖先 QScrollArea（分页改造后卡片在页内滚动区中）"""
        p = widget.parentWidget()
        while p is not None:
            if isinstance(p, QScrollArea):
                return p
            p = p.parentWidget()
        return None

    def refresh_style(self):
        """主题刷新：基类样式 + 左侧导航按钮 + 分页滚动条"""
        super().refresh_style()
        if self._nav_frame is not None:
            self._nav_frame.setStyleSheet(self._nav_frame_style())
        if hasattr(self, "_nav_buttons"):
            self._update_nav_styles()
        for page in getattr(self, "_page_scrolls", {}).values():
            page.setStyleSheet(SystemCardFrame._scroll_style())
            for sb in (page.verticalScrollBar(), page.horizontalScrollBar()):
                if sb is not None:
                    sb_style = sb.style()
                    sb_style.unpolish(sb)
                    sb_style.polish(sb)

    def _setup_appearance_cards(self):
        self.uiFontSizeCard = FontSizeStepperCard(
            get_icon("字体大小"),
            "界面字号",
            "统一调整界面与对话内容字号",
            self.cfg,
            self,
        )
        self.uiLightModeCard = SwitchSettingCard(
            get_icon("主题风格"),
            "浅色模式",
            "切换为浅色界面配色",
            self.cfg.ui_light_mode,
            self,
        )
        self.uiThemeStyleCard = self._make_theme_style_card()

    def _make_theme_style_card(self):
        """构建主题风格卡（展开式选项卡，与提示音卡一致的下部展开交互）

        validator options 与 texts 严格对齐（均为深浅过滤后的列表），
        防 OptionsSettingCard 内部 zip(texts, options) 错位。
        """
        options = self._build_theme_options()
        keys = list(options.keys())
        if not keys:
            return getattr(self, "uiThemeStyleCard", None)
        self.cfg.ui_theme_style.validator.__init__(keys)
        if self.cfg.ui_theme_style.value not in keys:
            self.cfg.set(self.cfg.ui_theme_style, keys[0], save=True)
        return OptionsSettingCard(
            self.cfg.ui_theme_style,
            get_icon("主题风格"),
            "主题风格",
            "选择界面卡片配色方案",
            texts=[d["label"] for d in options.values()],
            parent=self,
        )

    def _rebuild_theme_style_card(self) -> None:
        """按当前深浅模式重建主题风格卡（选项动态过滤），保持原布局位置"""
        old = getattr(self, "uiThemeStyleCard", None)
        new_card = self._make_theme_style_card()
        if new_card is None or new_card is old:
            return
        layout = self._page_layouts["appearance"]
        idx = layout.indexOf(old) if old is not None else -1
        if old is not None:
            layout.removeWidget(old)
            old.deleteLater()
        if idx >= 0:
            layout.insertWidget(idx, new_card)
        else:
            layout.addWidget(new_card)
        self.uiThemeStyleCard = new_card

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
        target_theme = "lumia" if is_light else "fallout"
        # 先重建 validator 为目标模式主题集（防旧集不含 target 被 correct 回退），
        # 当前值不在集内时回第一个（_make_theme_style_card 内处理）
        if self.cfg.ui_theme_style.value != target_theme:
            self.cfg.set(self.cfg.ui_theme_style, target_theme, save=True)
        # 重建主题风格卡（展开式选项按新模式过滤）
        self._rebuild_theme_style_card()
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

        # 字体/字号变更 → 即时重建左侧导航按钮 QSS
        # （按钮样式串内嵌 font_size_css/get_font_family_css，旧 QSS 会压制
        #   apply_font_size_to_widget 的 setFont；不重建则切 tab 前字体不生效）
        if LLMSettingsCard._last_change_type in ("font_size", "font_family"):
            try:
                self._update_nav_styles()
            except Exception:
                pass

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
        """分隔标签已随左侧导航改造移除，保留空实现兼容外部调用

        （main_widget 主题刷新链路 hasattr 检查本方法）
        """

    def refresh_theme_options(self):
        """热更新后刷新主题选项（外部由 _on_plugin_hot_reload 调用）"""
        try:
            self._rebuild_theme_style_card()
        except Exception as e:
            logger.warning(f"[ThemeStyleCard] 主题选项刷新失败: {e}")

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
