# -*- coding: utf-8 -*-
"""
大模型设置卡片 - 垂直列表布局，高度不够滚动
现已迁移到 SystemCardFrame 基类，获得统一头部布局和固定边框
"""

from loguru import logger
from PyQt5.QtCore import QPointF, QPropertyAnimation, QRectF, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen
from PyQt5.QtWidgets import (
    QFontComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBoxSettingCard,
    ExpandSettingCard,
    SearchLineEdit,
    OptionsSettingCard,
    PrimaryPushButton,
    RangeSettingCard,
    ScrollArea,
    SettingCard,
    SwitchSettingCard,
)
from app.utils.app_icon import AppIcon

from app.utils.config import Settings
from app.utils.design_tokens import (
    FONT_SIZE_OPTIONS,
    _DEFAULT_FONT_SIZE_KEY,
    _get_global_font,
    system_font_family,
    _LEGACY_FONT_SIZE_KEYS,
    Animations,
    ButtonStyles,
    Colors,
    ComboBoxStyles,
    apply_font_size_to_widget,
    font_size_css,
    get_ui_font_size,
    invalidate_font_cache,
    make_scroll_content_opaque,
    scale_icon_size,
)
from app.utils.startup_manager import (
    AutoStartCancelled,
    build_startup_command,
    get_registered_command,
    request_auto_start_update,
)
from app.utils.theme_mode import apply_light
from app.utils.scroll_tuning import install_fast_wheel
from app.utils.utils import get_font_family_css, get_icon, invalidate_font_family_css_cache
from app.widgets.cards.settings.list_setting_card import SkillListSettingCard
from app.widgets.cards.settings.plugin_components_card import PluginComponentsCard
from app.widgets.cards.settings.plugin_settings_page import PluginSettingsPage
from app.widgets.cards.settings.provider_setting_card import ProviderListSettingCard
from app.widgets.cards.settings.secret_mode_card import SecretModeSettingCard
from app.widgets.cards.settings.render_restart_card import RenderRestartCard
from app.widgets.cards.settings.theme_mode_card import ThemeModeSettingCard
from app.widgets.cards.settings.render_advanced_card import RenderAdvancedCard
from app.widgets.cards.settings.update_proxy_card import UpdateProxyCard
from app.widgets.cards.settings.render_backend_card import RenderBackendCard
from app.widgets.cards.settings.render_status_card import RenderStatusCard
from app.widgets.cards.settings.system_card_frame import SystemCardFrame
from app.widgets.cards.settings.theme_style_card import ThemeStyleCard, prepare_theme_entries
from app.widgets.cards.settings.theme_music_card import ThemeMusicCard


class NoWheelFontComboBox(QFontComboBox):
    """禁用滚轮切换的字体下拉框"""

    def wheelEvent(self, event):
        event.ignore()


class _PreferenceRow:
    """通用偏好用分隔线组织，控件继续使用 Fluent 的配置绑定。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hBoxLayout.setContentsMargins(0, 0, 0, 0)
        # 布局整体垂直居中会按单行 sizeHint 截短文字列，换行时改由弹性留白居中。
        self.hBoxLayout.setAlignment(Qt.Alignment())
        self.vBoxLayout.setAlignment(Qt.Alignment())
        self.vBoxLayout.insertStretch(0)
        self.vBoxLayout.addStretch()
        self.vBoxLayout.setSpacing(4)
        self.hBoxLayout.setStretchFactor(self.vBoxLayout, 8)
        self.vBoxLayout.setAlignment(self.contentLabel, Qt.Alignment())
        self.contentLabel.setWordWrap(True)
        self.setMinimumHeight(72)
        self.setMaximumHeight(16777215)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setObjectName("settingsPreferenceRow")
        self.setStyleSheet(f"QFrame#settingsPreferenceRow {{ border-bottom: 1px solid {Colors.DIVIDER_COLOR}; }}")

    def paintEvent(self, event):
        QFrame.paintEvent(self, event)


class _PreferenceSwitch(_PreferenceRow, SwitchSettingCard):
    def setValue(self, value):
        super().setValue(value)
        self.switchButton.setText("")
        self.switchButton.setAccessibleName(self.titleLabel.text())


class _PreferenceChoice(_PreferenceRow, ComboBoxSettingCard):
    pass


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
        """按最大档位实际字号(14+10=24px)预留标签行高与最小高度（防大字号标签被裁切）"""
        from PyQt5.QtGui import QFontMetrics

        fm = QFontMetrics(QFont(_get_global_font(), 24))
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

        # 节点 + 刻度标签（标签用对应档位实际字号渲染，直观体现大小差异）
        family = _get_global_font()
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

            # 刻度标签：用对应档位实际字号(14+delta)渲染，数字自身大小随档位变化
            actual = 14 + int(key)
            if is_cur or is_zero:
                fnt = QFont(family, actual)
                fnt.setBold(True)
                p.setFont(fnt)
                p.setPen(QColor(Colors.TEXT_ACCENT if is_cur else Colors.TEXT_PRIMARY))
            else:
                fnt = QFont(family, actual)
                p.setFont(fnt)
                p.setPen(QColor(Colors.TEXT_PRIMARY if is_hover else Colors.TEXT_SECONDARY))
            p.drawText(
                QRectF(x - 32, node_y + 8, 64, self._label_h),
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
        self._set_value_label_size(key)

    def _set_value_label_size(self, key: str) -> None:
        """header 当前值用对应档位实际字号渲染（数字自身大小随档位变化，直观体现差异）"""
        actual = 14 + int(key)
        self._value_label.setText(f"{_font_step_text(key)} px")
        self._value_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} font-size: {actual}px;"
        )

    def refresh_style(self) -> None:
        """主题/字号变更后重绘刻度条与文字样式（颜色/字号在 paint 动态读取）"""
        key = self.cfg.ui_font_size.value
        key = _LEGACY_FONT_SIZE_KEYS.get(key, key)
        if key not in FONT_SIZE_OPTIONS:
            key = _DEFAULT_FONT_SIZE_KEY
        self._set_value_label_size(key)
        self._hint_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)}"
        )
        # 字号变化 → 标签行高/最小高度重算（防大字号标签被裁切）+ 重绘
        self._track._recompute_metrics()


class ManualUpdateCard(SettingCard):
    def __init__(self, title, content, parent_widget, parent=None):
        super().__init__(AppIcon.SYNC, title, content, parent)
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

    # 左侧导航分组：(分组标题, ((tab_id, 显示名, 图标源), ...))
    # 图标源：字符串 → 主题感知资源图标 get_icon(name)；AppIcon 枚举 → 内置图标
    # 「插件」项按注册卡片动态显隐（_sync_plugin_nav_visibility）；
    # 插件注册的独立配置大 tab（register_settings_tab）动态插到「插件」项之后
    NAV_GROUPS = (
        (
            "模型与对话",
            (
                ("provider", "服务商", "大模型"),
                ("agents", "智能体", "智能体"),
                ("context", "上下文", "上下文"),
                ("skills", "技能", "技能"),
            ),
        ),
        (
            "插件与集成",
            (
                ("plugins", "插件设置", AppIcon.APPLICATION),
                ("tools", "工具", "工具"),
                ("hooks", "Hooks", "hooks"),
                ("mcp", "MCP", "MCP"),
                ("lsp", "LSP", "lsp"),
            ),
        ),
        (
            "界面",
            (
                ("appearance", "外观", "主题风格"),
                ("render", "渲染", AppIcon.SPEED_HIGH),
            ),
        ),
        (
            "系统",
            (
                ("common", "通用", AppIcon.SETTING),
                ("security", "安全中心", "锁定"),
                ("update", "更新", AppIcon.UPDATE),
            ),
        ),
    )

    # 搜索常用设置词无需提前构造各页的卡片。
    _NAV_KEYWORDS = {
        "provider": "模型 API Key 密钥 加密 Gitee 账号 OAuth",
        "agents": "agent 启用",
        "context": "预算 截断 压缩 落盘",
        "skills": "skill 启用",
        "plugins": "plugin 配置 权限",
        "tools": "tool 启用",
        "hooks": "钩子",
        "mcp": "服务器",
        "lsp": "语言服务器 代码诊断",
        "appearance": "主题 深色 浅色 字体 字号 音乐 动效",
        "render": "性能 内存 GPU WebGL 滚动 WebEngine",
        "common": "开机自启 单实例 通知 提示音 锁屏远程 消息身份 Enter 纯 Qt",
        "security": "沙箱 黑名单 白名单 删除保护 备份",
        "update": "版本 检查更新 下载代理 Beta",
    }

    _autostart_toggling = False  # 类级防重入标志
    _last_change_type: str | None = None  # "theme" | "font_family" | "font_size" | None(=全部)
    # 隐藏期主题刷新补刷协议标记（见 showEvent / GlobalCardController.refresh_theme_styles）。
    # 类级默认值必需：门控方用 hasattr 探测协议，实例属性首次赋值前 hasattr 为 False，
    # 会导致隐藏门控失效（2026-10-10 实测 gcc 门控因此未生效，226ms 白付）。
    _theme_needs_refresh: bool = False
    # 插件分区指纹的类级默认：`__new__` 造的桩（测试 fixture）不跑 __init__，
    # QObject 未初始化时读实例属性会抛 RuntimeError 而非 AttributeError → 必须给默认值
    _plugin_cards_sig: tuple | None = None
    # 同上：__new__ 桩（测试 fixture）不走 __init__，读该实例属性需类级默认兜底
    _plugin_cards_built: bool = False
    # 懒构造分页 → 构造方法：首开不同步构建（~340ms 主体），见 _setup_content 注释
    _LAZY_PAGE_BUILDERS = {
        "common": "_build_common_page",
        "render": "_build_render_page",
        "security": "_build_security_page",
        "appearance": "_build_appearance_page",
        "update": "_build_update_page",
        "mcp": "_build_mcp_page",
        "hooks": "_build_hooks_page",
        # 非首屏轻卡下沉（各 2~6ms + 数十控件）：用户可能整场不进这些页
        "lsp": "_build_lsp_page",
        "tools": "_build_tools_page",
        "agents": "_build_agents_page",
        "context": "_build_context_page",
    }
    closed = pyqtSignal()
    configChanged = pyqtSignal()
    # MCP 页懒态信号转发（#35）：主卡构造期 mcpListCard=None（MCP 页懒构造），
    # controller（ensure_settings_popup）接线时连主卡信号永存在；卡片在
    # _build_mcp_page 构建时把自身信号转发到主卡信号——懒态不炸 + 进页后信号链通
    showAddCard = pyqtSignal()
    showEditCard = pyqtSignal(str, dict)
    # Hooks 页懒态信号转发（#39b）：hookListCard 同为懒构造，controller 的
    # 四条接线（添加/编辑/变更/单项开关）全部改连主卡转发信号；卡片在
    # _build_hooks_page 构建时把自身信号转发到主卡信号
    showAddHookCard = pyqtSignal()
    showEditHookCard = pyqtSignal(str, dict)
    hooksChanged = pyqtSignal()
    hookToggled = pyqtSignal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.set_icon_svg("配置管理")
        self.set_title_text("系统设置")
        self.setMinimumHeight(250)  # 自适应窗口高度，showEvent 会自动设置 maximumHeight

        self.cfg = Settings.get_instance()

        # 左侧导航 + 右侧分页：分区归属见 _setup_content
        self._current_tab = "provider"
        self._nav_frame = None  # _build_side_nav 中创建
        # 插件设置分区指纹：清单未变时跳过「销毁 + 重建」整片插件卡
        # （历史上每次打开设置面板都无条件重建，见 rebuild_plugin_cards）
        self._plugin_cards_sig = None
        # 插件设置分区是否已真正实例化（未实例化时只同步导航显隐，不构造 widget）
        self._plugin_cards_built = False
        # 插件注册的独立设置大 tab：当前挂载清单与已实例化页面（懒构造，见
        # _sync_plugin_tabs / _build_plugin_tab_page）
        self._plugin_tab_infos: dict = {}
        self._plugin_tab_pages: dict = {}
        # 已"就绪"的分页（首次进入时付一次性准备成本，见 _ensure_page_ready）
        self._page_ready: set[str] = set()

        self._setup_content()

        # 初始化时应用配置中的字体大小和主题样式
        QTimer.singleShot(0, self._refresh_appearance_from_config)
        # 首屏（服务商页）默认展开已配置的服务商列表
        # ★ 守卫：测试/快速销毁场景下回调泵出时 popup C++ 对象可能已删
        # （构造越快回调越晚于销毁，2026-10-30 实测 QVBoxLayout deleted）
        QTimer.singleShot(0, self._expand_provider_page_on_ready)

    def _expand_provider_page_on_ready(self):
        """singleShot 兜底：展开服务商页（对象已销毁时静默跳过）"""
        try:
            self._expand_page_cards("provider")
        except RuntimeError:
            return  # C++ 对象已销毁（deleteLater 后事件泵送触发）

    def _setup_content(self):
        content_layout = self.content_layout
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # ── 主体：左侧导航 + 右侧分页 ──
        tabs = [(tab_id, name) for _group, items in self.NAV_GROUPS for tab_id, name, _icon in items]
        body = QWidget(self)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(8, 4, 12, 8)
        body_layout.setSpacing(18)
        body_layout.addWidget(self._build_side_nav())

        self._pages_stack = QStackedWidget(self)
        self._page_scrolls = {}
        self._page_layouts = {}
        for tab_id, _name in tabs:
            page, page_layout = self._make_page()
            self._page_scrolls[tab_id] = page
            self._page_layouts[tab_id] = page_layout
            self._pages_stack.addWidget(page)
        # 页标题固定在滚动区上方，插件工作区共用此处的导航返回入口。
        self._back_btn = QToolButton(self)
        self._back_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._back_btn.setIcon(AppIcon.RETURN.qicon())
        self._back_btn.setIconSize(QSize(16, 16))
        self._back_btn.setText("显示设置导航")
        self._back_btn.setFixedHeight(32)
        self._back_btn.setCursor(Qt.PointingHandCursor)
        self._back_btn.setStyleSheet(self._back_btn_style())
        self._back_btn.hide()
        self._back_btn.clicked.connect(self._toggle_plugin_navigation)
        self._page_title = QLabel(self)
        self._page_title.setTextFormat(Qt.PlainText)
        self._page_title.setStyleSheet(self._page_title_style())
        bar_layout = QHBoxLayout()
        bar_layout.setContentsMargins(6, 8, 8, 8)
        bar_layout.setSpacing(12)
        bar_layout.addWidget(self._back_btn)
        bar_layout.addWidget(self._page_title, 1)
        pages_column = QWidget(self)
        pages_col_layout = QVBoxLayout(pages_column)
        pages_col_layout.setContentsMargins(0, 0, 0, 0)
        pages_col_layout.setSpacing(4)
        pages_col_layout.addLayout(bar_layout)
        pages_col_layout.addWidget(self._pages_stack, 1)
        body_layout.addWidget(pages_column, 1)
        content_layout.addWidget(body)
        self._update_nav_styles()

        # ════ 服务商页 ════
        provider_layout = self._page_layouts["provider"]

        # Gitee 账号绑定（保持原默认页顶部位置）
        # [PERF L3] GiteeCard 链（gateway.auth + requests ~155ms）下沉到构造期局部
        # import：顶层 import 曾在「点设置同步 import 主卡」时拉链（plan #29 实测
        # import 909ms 大头之一）。构造时才拉，点开设置的 import 段变快。
        from app.widgets.cards.settings.gitee_card import GiteeCard

        self.giteeCard = GiteeCard(self)
        provider_layout.addWidget(self.giteeCard)

        # API Key 加密方式（系统钥匙串 / 密码加密 / 不加密）
        self.secretModeCard = SecretModeSettingCard(self)
        provider_layout.addWidget(self.secretModeCard)

        self.llmProviderCard = ProviderListSettingCard(
            icon=get_icon("大模型"),
            configItem=self.cfg.llm_saved_providers,
            defaultProviderItem=self.cfg.llm_selected_model,
            title="已保存的服务商",
            content="管理已配置的大模型服务商",
            parent=self,
            home=self,
        )
        provider_layout.addWidget(self.llmProviderCard)
        provider_layout.addStretch(1)

        # ════ Hooks 页 ════
        # [PERF L3/#34] Hooks 页整体下沉懒构造（同 MCP 页模式）：hook_setting_card
        # 模块首次加载 + HookListSettingCard 构造实测 ~400ms，随主卡构造启动纯属
        # 浪费（用户可能整场会话不进 Hooks 页）。首次进页时才构建。
        self.hookListCard = None

        # ════ MCP 页 ════
        # [PERF L3] MCP 页整体下沉懒构造：mcp_setting_card 顶层 import app.tools.mcp_tools
        # = mcp SDK 全家桶（plan #29 实测 cum 598.9ms，含 mcp-eventloop 常驻线程），
        # 随主卡构造启动纯属浪费。改首次进入 MCP 页时才构建（_ensure_page_ready），
        # ConfigSync/mcp-eventloop 等常驻资源同步推迟到进入页后。
        self.mcpListCard = None

        # ════ LSP / 工具 / 智能体 / 上下文 四页：卡片下沉懒构造 ════
        # 四张卡各自 2~6ms + 数十控件，用户可能整场不进这些页 → 首次进页才建
        # （_ensure_page_ready → _build_*_page）。占位 None 让外部 getattr 探测安全。
        self.lspListCard = None
        self.pluginToolCard = None
        self.pluginAgentCard = None
        self.pluginContextCard = None

        # 技能启用（按插件/内置/用户分组，行在展开后分批构建）
        skills_layout = self._page_layouts["skills"]
        self.llmSkillsCard = SkillListSettingCard(
            icon=get_icon("智能体"),
            configItem=self.cfg.llm_enabled_skills,
            title="技能启用",
            content="选择要注入的技能",
            parent=self,
            home=self,
        )
        skills_layout.addWidget(self.llmSkillsCard)
        skills_layout.addStretch(1)

        # ════ 通用/渲染/安全中心/外观/更新 五页懒构造 ════
        # 首开设置卡只同步构建 provider + 插件类轻页（首屏必需或骨架轻量）；
        # 上述五页合计 ~300ms（2026-10-30 watchdog 现场 514ms 的主体），挪到
        # 首次进页（_ensure_page_ready）或显示后空闲帧（_prefetch_pages 错峰）。

        # ════ 插件设置页（主从：全部插件清单 + 配置/权限/关于 详情）════
        # 容器也下沉懒建（实测 5.7ms + 筛选 chips 一片控件）：首次进入插件页时
        # 由 rebuild_plugin_cards → _ensure_plugins_page 建，随即 reload 列表数据。
        self.pluginSettingsPage = None
        self._plugin_cards_widget = None  # 兼容既有可见性控制（未建时为 None）
        # 左侧导航：按「有无插件」显隐（只查清单不构造行；列表数据延迟到
        # 首次进入该页 reload，见 _ensure_page_ready）
        self._sync_plugin_nav_visibility()
        # 插件注册的独立设置大 tab：同步导航按钮与分页（懒实例化）
        self._sync_plugin_tabs()

        # 连接信号
        # 注意：只有真正影响外观的变更才走 _on_config_changed（触发全量刷新）
        self.cfg.ui_font_family.valueChanged.connect(self._on_config_changed)
        self.cfg.llm_font_family.valueChanged.connect(self._on_config_changed)
        self.cfg.ui_font_size.valueChanged.connect(self._on_config_changed)
        self.cfg.ui_theme_style.valueChanged.connect(self._on_config_changed)
        self.cfg.ui_light_mode.valueChanged.connect(self._on_light_mode_changed)
        self.cfg.llm_api_enabled.valueChanged.connect(self._on_llm_api_enabled_changed)
        self.cfg.llm_api_port.valueChanged.connect(self._on_llm_api_port_changed)

        # 列表形式配置卡片手风琴：展开一个时自动收起其他
        # 懒构造页的卡片在各自 builder 里经 _register_list_card 增量补入
        self._list_cards = [c for c in (self.llmProviderCard, self.llmSkillsCard) if c is not None]
        self._apply_list_accordion()

    # ── 懒构造分页（首次进页/空闲帧预热时才构建，见 _ensure_page_ready）───

    def _build_common_page(self):
        """通用设置页：开关与通知卡"""
        common_layout = self._page_layouts["common"]
        common_layout.setContentsMargins(16, 4, 16, 12)
        common_layout.setSpacing(0)
        self._common_sections = []

        def add_section(text):
            label = QLabel(text, self)
            label.setStyleSheet(self._nav_group_style())
            common_layout.addWidget(label)
            self._common_sections.append(label)

        add_section("启动与运行")
        # 锁屏远程
        self.lockRemoteCard = _PreferenceSwitch(
            AppIcon.SYNC,
            "锁屏远程",
            "保持系统唤醒、屏幕常亮。",
            configItem=self.cfg.lock_screen_remote_enabled,
            parent=self,
        )
        self.lockRemoteCard.checkedChanged.connect(self._on_lock_remote_toggled)
        common_layout.addWidget(self.lockRemoteCard)

        # 开机自启
        self.autoStartCard = _PreferenceSwitch(
            get_icon("开机自动启动"),
            "开机自启",
            "系统启动时自动运行 Drifox",
            self.cfg.auto_start,
            self,
        )
        self.autoStartCard.checkedChanged.connect(self._on_toggled)
        common_layout.addWidget(self.autoStartCard)

        # 单实例限制：同时只允许运行一个实例
        self.singleInstanceCard = _PreferenceSwitch(
            AppIcon.LAYOUT,
            "单实例限制",
            "开启后限制一个 Drifox 实例，重启生效",
            configItem=self.cfg.enable_single_instance,
            parent=self,
        )
        common_layout.addWidget(self.singleInstanceCard)

        add_section("消息与输入")

        # 消息身份行：每条消息顶部显示发送者头像 + 名称（插件可覆盖身份）
        self.messageIdentityCard = _PreferenceSwitch(
            AppIcon.PEOPLE,
            "消息身份行",
            "每条消息显示发送者头像与名称",
            configItem=self.cfg.ui_message_identity,
            parent=self,
        )
        common_layout.addWidget(self.messageIdentityCard)

        # 灰度开关：纯 Qt 消息渲染器（MarkdownBlockViewer 替代 QWebEngineView）
        self.qtRendererCard = _PreferenceSwitch(
            AppIcon.CODE,
            "纯 Qt 消息渲染（实验）",
            "消息正文改用原生控件渲染，显著降低内存占用",
            configItem=self.cfg.qt_message_renderer,
            parent=self,
        )
        common_layout.addWidget(self.qtRendererCard)

        # 繁忙时 Enter 键行为：智能体运行时按 Enter 的动作（Ctrl+Enter 恒为另一行为）
        self.busyEnterCard = _PreferenceChoice(
            self.cfg.busy_enter_behavior,
            AppIcon.SEND,
            "繁忙时 Enter 键行为",
            "仅在智能体运行时生效；Ctrl+Enter 使用另一行为",
            texts=["插话发送", "排队发送"],
            parent=self,
        )
        common_layout.addWidget(self.busyEnterCard)

        # ── 通知设置（并入通用页）──
        add_section("通知")
        # 智能体完成通知
        self.llmNotifyCard = _PreferenceSwitch(
            get_icon("提示"),
            "智能体完成通知",
            "窗口不在前台时发送通知",
            configItem=self.cfg.llm_notify_enabled,
            parent=self,
        )
        common_layout.addWidget(self.llmNotifyCard)

        # 通知提示音
        self.llmSoundCard = _PreferenceChoice(
            self.cfg.llm_notify_sound,
            get_icon("提示"),
            "通知提示音",
            "选择提示音",
            texts=["默认", "短提示音", "无"],
            parent=self,
        )
        common_layout.addWidget(self.llmSoundCard)

        # 进入时崩溃通知
        self.crashNotifyCard = _PreferenceSwitch(
            get_icon("提示"),
            "崩溃通知",
            "检测到上次异常退出时弹出提示横幅",
            configItem=self.cfg.crash_notify_on_startup,
            parent=self,
        )
        common_layout.addWidget(self.crashNotifyCard)
        common_layout.addStretch(1)

    def _build_render_page(self):
        """渲染与性能页：WebEngine 环境变量配置卡（全部重启生效）"""
        render_layout = self._page_layouts["render"]
        # 换算逻辑见 app/utils/render_env.py；高级项（DisabledFeatures /
        # ExtraChromiumFlags）不进 UI，走 app.config [Render] 组直达。

        # 手动重启（首项）：本页全部配置项都是 QtWebEngine 启动时一次性读取的
        # 环境变量，运行中改无效。卡片同时承担「待生效变更」提示，监控对象为
        # 下面所有 Render 组 ConfigItem（含未进 UI 的两个高级项）。
        # 当前生效参数：回显本次进程实际跑的那组值，用于核对「改了有没有生效」
        # ── Render 组配置项清单（两张卡共用：重启卡的变更计数、回显卡的恢复默认）──
        render_items = [
            self.cfg.render_backend,
            self.cfg.render_webgl,
            self.cfg.render_renderer_process_limit,
            self.cfg.render_js_heap_mb,
            self.cfg.render_low_end_device_mode,
            self.cfg.render_smooth_scrolling,
            self.cfg.render_canvas_aa,
            self.cfg.render_disable_background_throttling,
            self.cfg.render_share_gl_contexts,
            self.cfg.render_disabled_features,
            self.cfg.render_extra_flags,
        ]

        self.renderRestartCard = RenderRestartCard(
            render_items=render_items,
            parent=self,
        )
        render_layout.addWidget(self.renderRestartCard)

        self.renderStatusCard = RenderStatusCard(
            render_items=render_items,
            parent=self,
        )
        render_layout.addWidget(self.renderStatusCard)

        # 渲染后端：Qt/Chromium 图形栈档位（说明随所选档位变化，见 render_backend_card）
        self.renderBackendCard = RenderBackendCard(
            self.cfg.render_backend,
            # ⚠️ 顺序必须与 render_backend 的 OptionsValidator 逐一对应
            texts=[
                "软件 (WARP)",
                "硬件 (D3D11)",
                "软件 GL (最稳)",
                "Vulkan (排障)",
                "D3D9 (老机器)",
                "SwiftShader (双保险)",
            ],
            parent=self,
        )
        render_layout.addWidget(self.renderBackendCard)

        # WebGL 按需解禁（3D 图形需要）
        self.renderWebglCard = OptionsSettingCard(
            self.cfg.render_webgl,
            AppIcon.GLOBE,
            "WebGL / 3D 图形",
            "关闭可省 GPU 进程内存",
            texts=["自动", "开", "关"],
            parent=self,
        )
        render_layout.addWidget(self.renderWebglCard)

        # Chromium renderer 进程硬上限（内存治理核心项）
        self.renderProcessLimitCard = RangeSettingCard(
            self.cfg.render_renderer_process_limit,
            AppIcon.LAYOUT,
            "Renderer 进程上限",
            "消息卡片渲染进程数硬上限",
            parent=self,
        )
        self.renderProcessLimitCard.slider.setFixedWidth(160)  # qfluentwidgets 默认 minWidth=268，压缩滑条长度
        render_layout.addWidget(self.renderProcessLimitCard)

        # 单 renderer JS 堆上限
        self.renderJsHeapCard = RangeSettingCard(
            self.cfg.render_js_heap_mb,
            AppIcon.CLOUD,
            "单卡片 JS 堆上限 (MB)",
            "限制单张消息卡片的内存",
            parent=self,
        )
        self.renderJsHeapCard.slider.setFixedWidth(160)
        render_layout.addWidget(self.renderJsHeapCard)

        # Chromium 低内存模式
        self.renderLowEndCard = SwitchSettingCard(
            AppIcon.REMOVE_FROM,
            "低内存模式",
            "压低渲染缓冲/缓存，抗锯齿略降",
            configItem=self.cfg.render_low_end_device_mode,
            parent=self,
        )
        render_layout.addWidget(self.renderLowEndCard)

        # 合成器平滑滚动
        self.renderSmoothCard = SwitchSettingCard(
            AppIcon.TILES,
            "平滑滚动",
            "卡内滚动的合成器动画",
            configItem=self.cfg.render_smooth_scrolling,
            parent=self,
        )
        render_layout.addWidget(self.renderSmoothCard)

        # 后台渲染节流（长对话离屏卡片被降优先级 → 流式卡顿的解药）
        self.renderThrottleCard = SwitchSettingCard(
            AppIcon.PAUSE,
            "关闭后台渲染节流",
            "离屏卡片不再被降优先级，抗流式卡顿",
            configItem=self.cfg.render_disable_background_throttling,
            parent=self,
        )
        render_layout.addWidget(self.renderThrottleCard)

        # 2D canvas 抗锯齿
        self.renderCanvasAACard = SwitchSettingCard(
            AppIcon.BRUSH,
            "Canvas 抗锯齿",
            "开启后 echarts 图表边缘更平滑",
            configItem=self.cfg.render_canvas_aa,
            parent=self,
        )
        render_layout.addWidget(self.renderCanvasAACard)

        # 共享 GL 上下文：省约 12.7% 内存；共用一个上下文被怀疑与多卡/图表闪烁相关
        self.renderShareGLCard = SwitchSettingCard(
            AppIcon.LAYOUT,
            "共享 GL 上下文",
            "省约 12% 内存；渲染闪烁时可尝试关闭",
            configItem=self.cfg.render_share_gl_contexts,
            parent=self,
        )
        render_layout.addWidget(self.renderShareGLCard)

        # ── 高级配置（折叠）：一条一项 + 右侧开关，扁平列表不分组；
        # 底层仍写回 DisabledFeatures / ExtraChromiumFlags（此前只能手改 app.config）──
        self.renderAdvancedCard = RenderAdvancedCard(
            feature_item=self.cfg.render_disabled_features,
            flag_item=self.cfg.render_extra_flags,
            parent=self,
        )
        render_layout.addWidget(self.renderAdvancedCard)
        render_layout.addStretch(1)

    def _build_security_page(self):
        """安全中心页：沙箱开关 / 文件与命令名单 / 网络黑名单 / 删除保护 / 备份"""
        security_layout = self._page_layouts["security"]
        from app.widgets.cards.settings.security_center_card import SecurityCenterCard

        self.securityCenterCard = SecurityCenterCard()
        security_layout.addWidget(self.securityCenterCard)
        security_layout.addStretch(1)

    def _build_appearance_page(self):
        """外观样式页：浅色/主题风格/字号/字体/对话区宽度"""
        appearance_layout = self._page_layouts["appearance"]

        # 浅色模式、主题风格
        self._setup_appearance_cards()
        appearance_layout.addWidget(self.uiThemeModeCard)
        appearance_layout.addWidget(self.uiThemeStyleCard)

        # 主题音乐（无音乐的主题整卡隐藏；展开看歌单/单曲开关/当前播放）
        self.uiMusicCard = ThemeMusicCard(self.cfg.ui_music_volume, parent=self)
        # 配置项级监听：卡内滑块与其他入口（配置文件/云同步）都能同步到播放器
        self.cfg.ui_music_volume.valueChanged.connect(self._on_music_volume_changed)
        appearance_layout.addWidget(self.uiMusicCard)

        # 系统关闭「动画效果」时的逃生口：全仓动效统一查 Animations.motion_enabled()
        self.uiMotionCard = SwitchSettingCard(
            AppIcon.SPEED_HIGH,
            "始终播放动效",
            "系统关闭动画效果时仍播放过渡动画",
            configItem=self.cfg.ui_motion_always_on,
            parent=self,
        )
        self.uiMotionCard.checkedChanged.connect(self._on_motion_override_changed)
        appearance_layout.addWidget(self.uiMotionCard)

        # 界面字号 + 全局字体
        appearance_layout.addWidget(self.uiFontSizeCard)
        self._setup_font_card()
        appearance_layout.addWidget(self.uiFontCard)
        appearance_layout.addWidget(self.llmFontCard)

        # 对话区宽度
        appearance_layout.addWidget(self.uiChatWidthCard)
        appearance_layout.addStretch(1)

    def _build_update_page(self):
        """版本更新页：自动检查 / Beta / 手动检查 / 下载代理"""
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

        # 接收 Beta 版更新（含 prerelease）
        self.betaUpdateCard = SwitchSettingCard(
            AppIcon.UPDATE,
            "接收 Beta 版更新",
            "检查更新时包含 Beta 预览版",
            configItem=self.cfg.allow_beta_update,
            parent=self,
        )
        update_layout.addWidget(self.betaUpdateCard)

        self.manualUpdateCard = ManualUpdateCard(
            "手动检查更新",
            "点击按钮检查是否有新版本",
            self.parent(),
            self.parent(),
        )
        update_layout.addWidget(self.manualUpdateCard)

        # 下载代理配置（四选一：直连 / 跟随系统 / 加速前缀 / 手动代理）
        self.updateProxyCard = UpdateProxyCard(self)
        update_layout.addWidget(self.updateProxyCard)

        update_layout.addStretch(1)

    def _apply_appearance_to_page(self, tab_id: str):
        """懒构造页建好后立即对齐当前全局外观（字号/主题刷新只覆盖已存在的控件）"""
        page = self._page_scrolls.get(tab_id)
        if page is None:
            return
        try:
            apply_font_size_to_widget(page, get_ui_font_size())
            icon_sz = scale_icon_size(16)
            for card in page.findChildren(SettingCard):
                card.setIconSize(icon_sz, icon_sz)
            for frame in page.findChildren(SystemCardFrame):
                if hasattr(frame, "refresh_style"):
                    frame.refresh_style()
        except Exception as e:  # noqa: BLE001 - 外观对齐失败不阻断页内容
            logger.warning(f"[LLMSettingsCard] {tab_id} 页外观对齐失败: {e}")

    def rebuild_plugin_cards(self, force: bool = False):
        """刷新插件设置页（幂等）

        数据源是 PluginManager 全量插件清单（含没有 config_schema 的插件，
        它们仍可查看权限与关于），不再是「有设置卡的插件」子集。
        首次构建延迟到「插件设置页首次进入」：读清单 + 60+ 行挂载是一笔一次性
        开销，不该在打开设置的那一帧同步付。
        """
        self._sync_plugin_tabs()
        if not force and not self._plugin_cards_built:
            # 未进过插件页：容器与列表都还不该建（toggle_settings 每次打开都会走到这里）
            self._sync_plugin_nav_visibility()
            return
        self._ensure_plugins_page()
        if not self._sync_plugin_nav_visibility():
            return
        self._plugin_cards_built = True
        try:
            self.pluginSettingsPage.reload(force=force)
        except Exception as e:
            logger.warning(f"[LLMSettingsCard] 插件设置页刷新失败: {e}")

    def _build_mcp_page(self):
        """MCP 页懒构造（首次进入时才拉 mcp_setting_card 模块 = mcp SDK 全家桶）。

        下沉收益：主卡 import/构造不再拉 mcp SDK（cum 598.9ms）与 mcp-eventloop
        常驻线程——用户可能整场会话不进 MCP 页。构造后补入手风琴列表（单卡增量
        包装，防 _apply_list_accordion 整表重跑造成 setExpand 套娃）。
        """
        from app.widgets.cards.settings.mcp_setting_card import MCPListSettingCard

        mcp_layout = self._page_layouts["mcp"]
        self.mcpListCard = MCPListSettingCard(
            icon=get_icon("MCP"),
            title="MCP 服务器",
            content="管理 MCP Server 连接",
            parent=self,
        )
        mcp_layout.addWidget(self.mcpListCard)
        mcp_layout.addStretch(1)
        self._list_cards.append(self.mcpListCard)
        self._wrap_list_card_accordion(self.mcpListCard)
        # 信号链补接（#35）：卡片构建时把自身信号转发到主卡信号——controller 在
        # ensure_settings_popup 接线期连的是主卡信号（懒态安全），进页后链路接通
        self.mcpListCard.showAddCard.connect(self.showAddCard)
        self.mcpListCard.showEditCard.connect(self.showEditCard)

    def _build_hooks_page(self):
        """Hooks 页懒构造（首次进入时才拉 hook_setting_card 模块 + HookListSettingCard）。

        [PERF L3/#34] 原构造期建卡（模块加载 + 实例化实测 ~400ms）。
        含原 hooks 分支的 HookManager 注入与首刷（原 _ensure_page_ready elif
        hooks 逻辑并入）：构造期 parent 无 backend 时注入空 HookManager。
        """
        from app.widgets.cards.settings.hook_setting_card import HookListSettingCard

        hooks_layout = self._page_layouts["hooks"]

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
        hooks_layout.addWidget(self.hookListCard)
        hooks_layout.addStretch(1)
        self._list_cards.append(self.hookListCard)
        self._wrap_list_card_accordion(self.hookListCard)
        # 信号链补接（#39b）：构建时把卡片四条信号转发到主卡信号（controller
        # 接线期连主卡信号永存在，进 Hooks 页后链路接通——同 #35 MCP 模式）
        self.hookListCard.showAddHookCard.connect(self.showAddHookCard)
        self.hookListCard.showEditHookCard.connect(self.showEditHookCard)
        self.hookListCard.hooksChanged.connect(self.hooksChanged)
        self.hookListCard.hookToggled.connect(self.hookToggled)
        # 原 hooks 懒分支：构造期 hook_manager 为 None 时注入空 HookManager + 首刷
        if getattr(self.hookListCard, "_hook_manager", None) is None:
            from app.core.hooks.hook_manager import HookManager

            self.hookListCard._hook_manager = HookManager()
        self.hookListCard._refresh(reload=True)

    def _ensure_plugins_page(self):
        """插件设置页容器懒建（幂等）：首次进插件页时才建列表及筛选控件

        容器下沉收益：实测 5.7ms + 一片 chips 控件。列表数据仍由
        rebuild_plugin_cards 随后的 reload 拉取，两段成本都推给首次进页。
        """
        if getattr(self, "pluginSettingsPage", None) is not None:
            return
        page = PluginSettingsPage(self)
        self.pluginSettingsPage = page
        self._plugin_cards_widget = page  # 兼容既有可见性控制
        self._page_layouts["plugins"].addWidget(page, 1)
        self._sync_plugin_nav_visibility()

    def _register_list_card(self, card):
        """懒构造卡片补入手风琴表 + 单卡包装（增量，防已包卡重复包）"""
        self._list_cards.append(card)
        self._wrap_list_card_accordion(card)

    def _build_lsp_page(self):
        """LSP 页懒构造：LspListSettingCard 构造 + 首刷（首次进页才付）"""
        from app.widgets.cards.settings.lsp_setting_card import LspListSettingCard

        lsp_layout = self._page_layouts["lsp"]
        self.lspListCard = LspListSettingCard(
            icon=get_icon("lsp"),
            title="LSP 语言服务器",
            content="代码智能与诊断",
            parent=self,
        )
        lsp_layout.addWidget(self.lspListCard)
        lsp_layout.addStretch(1)
        self._register_list_card(self.lspListCard)

    # 工具/智能体/上下文 三页同构：只差 components 维度与文案/图标
    _PLUGIN_COMPONENT_SPECS = {
        "tools": (
            "pluginToolCard",
            ("tools",),
            "工具启用",
            "按插件控制其工具的启停",
            AppIcon.DEVELOPER_TOOLS,
        ),
        "agents": ("pluginAgentCard", ("agents",), "智能体启用", "按插件控制其智能体的启停", None),
        "context": (
            "pluginContextCard",
            ("context_tiers", "budget_resolvers"),
            "上下文管理",
            "按插件控制上下文层（截断 / 落盘 / 压缩）的启停",
            None,
        ),
    }

    def _build_plugin_components_page(self, tab_id: str):
        """工具/智能体/上下文页懒构造：PluginComponentsCard 构造（首刷由调用方补）"""
        attr, components, title, content, icon = self._PLUGIN_COMPONENT_SPECS[tab_id]
        if icon is None:
            icon = get_icon("智能体" if tab_id == "agents" else "上下文")
        layout = self._page_layouts[tab_id]
        card = PluginComponentsCard(
            components=components,
            title=title,
            content=content,
            icon=icon,
            parent=self,
        )
        setattr(self, attr, card)
        layout.addWidget(card)
        layout.addStretch(1)
        self._register_list_card(card)

    def _build_tools_page(self):
        self._build_plugin_components_page("tools")

    def _build_agents_page(self):
        self._build_plugin_components_page("agents")

    def _build_context_page(self):
        self._build_plugin_components_page("context")

    def _apply_list_accordion(self):
        """为列表形式配置卡片应用手风琴效果

        包装每个 ExpandSettingCard 子类的 setExpand 方法：展开某张卡片时，
        自动收起其他已展开的列表卡片，并把当前卡片的 header 滚到弹窗顶部。
        点击已展开卡片头部仍可正常收起。
        """
        for card in self._list_cards:
            self._wrap_list_card_accordion(card)

    def _wrap_list_card_accordion(self, card):
        """单卡手风琴包装（增量入口：懒构造分页补建时只包新卡，防套娃）"""
        original_set_expand = card.setExpand

        def _siblings():
            # 实时求值：懒构造页的卡片在包装之后才入场，快照会让后入场兄弟
            # 不被「展开一个收起其他」覆盖（手风琴语义缺失）
            return [c for c in self._list_cards if c is not card]

        def _wrapped_set_expand(is_expand, _orig=original_set_expand, _sibs=_siblings, _card=card):
            if is_expand:
                for sib in _sibs():
                    if sib.isExpand:
                        sib.setExpand(False)
            _orig(is_expand)
            # 展开后等动画/布局稳定，再把卡片内的「焦点 item」滚到弹窗顶部
            # 例如：ProviderListSettingCard 滚到默认 provider；其他卡片 fallback 到第一项
            if is_expand:
                # qfluentwidgets 展开动画通常 200ms+，等动画结束再算坐标
                QTimer.singleShot(350, lambda c=_card: self._scroll_focus_item_to_top(c))

        card.setExpand = _wrapped_set_expand

        # 两张开关卡都保持折叠（用户要求），点标题展开即可。
        # 若以后要恢复默认展开，必须在包装完成之后调用 setExpand——
        # 那样走的是包装版，手风琴语义才成立（会先收起其他已展开的列表卡片）。

    def _ensure_hot_reload_connected(self):
        """订阅插件热重载广播，让设置面板在热重载后就地刷新

        ⚠️ 不能在 __init__ / _apply_list_accordion 里调：那会触发
        PluginHostService 单例的惰性初始化，连带拉起 AgentManager 全量加载
        智能体并启动 watchfiles 监听（实测 ~330ms，且是不必要的副作用）。
        放到首次显示阶段，此时服务早已由主窗口初始化完毕。

        面板不可见时跳过重建——下次打开时 rebuild_plugin_cards 会因内容
        签名变化自动刷新，没必要在后台付这份开销。
        连续热重载用 350ms 去抖合并，避免一次批量更新触发多次全量重建。
        """
        if getattr(self, "_hot_reload_connected", False):
            return
        try:
            from app.core.services.plugin_host_service import PluginHostService

            self._hot_reload_connected = True
            self._hot_reload_timer = QTimer(self)
            self._hot_reload_timer.setSingleShot(True)
            self._hot_reload_timer.setInterval(350)
            self._hot_reload_timer.timeout.connect(self._refresh_after_hot_reload)
            PluginHostService.get_instance().plugin_changed.connect(self._on_plugin_changed, Qt.UniqueConnection)
        except Exception as e:
            logger.warning(f"[LLMSettingsCard] 订阅插件热重载广播失败: {e}")

    def _on_plugin_changed(self, payload: dict):
        """插件变更广播回调（去重后统一刷新）"""
        if not self.isVisible():
            return
        self._hot_reload_timer.start()

    def _prefetch_skills(self):
        """技能发现预热：只在进技能页时跑（原 showEvent 的 300ms 定时预热已下线）"""
        try:
            card = getattr(self, "llmSkillsCard", None)
            if card is None or getattr(card, "_discovered", True):
                return
            card._discover_skills()
            card._update_skill_token_count()
        except Exception as e:
            logger.debug(f"[LLMSettingsCard] 技能发现预热失败: {e}")

    def _refresh_after_hot_reload(self):
        if not self.isVisible():
            return
        try:
            # force=True：热重载后插件设置卡必须重建，避免卡片实例仍持有旧类；
            # 且插件可能只改了卡内部实现（清单指纹不变），跳过就刷新不到。
            self.rebuild_plugin_cards(force=True)
            # force=True：热重载可能只改了组件内部的细项（工具/智能体增删），
            # 插件清单未变 → 签名相同 → 非 force 的脏检查会跳过，必须强制重建
            # 懒构造：卡片未建的页跳过（首次进页时 refresh_components 自己会付）
            for card_name in ("pluginToolCard", "pluginAgentCard"):
                card = getattr(self, card_name, None)
                if card is not None:
                    card.refresh_components(force=True)
            logger.info("[LLMSettingsCard] 插件热重载后已刷新设置面板")
        except Exception as e:
            logger.warning(f"[LLMSettingsCard] 热重载后刷新设置面板失败: {e}")

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
                return
            content_widget = scroll_area.widget()
            if content_widget is None:
                return
            # ExpandSettingCard 自带的 header widget（含图标/标题/展开按钮）
            header_widget = getattr(card, "card", None)
            if header_widget is None:
                return
            doc_y = header_widget.mapTo(content_widget, QPoint(0, 0)).y()
            # header 顶部对齐视窗顶部，留 5px 边距
            target = max(0, doc_y - 5)
            scroll_area.verticalScrollBar().setValue(target)
        except Exception:
            pass

    # ── 左侧导航 + 分页 ──────────────────────────────

    def _build_side_nav(self) -> QFrame:
        """构建左侧导航面板：分组标题 + 图标导航项，顶部对齐，超高可滚动"""
        self._nav_buttons = {}
        self._nav_group_labels = []
        self._nav_order = []  # 页序（供相邻页预热定位）
        nav = QFrame(self)
        nav.setObjectName("settingsSideNav")
        nav.setStyleSheet(self._nav_frame_style())

        outer = QVBoxLayout(nav)
        outer.setContentsMargins(5, 7, 6, 4)
        outer.setSpacing(5)
        self._nav_search = SearchLineEdit(nav)
        self._nav_search.setPlaceholderText("查找")
        self._nav_search.setAccessibleName("查找设置页面")
        self._nav_search.setClearButtonEnabled(True)
        self._nav_search.returnPressed.connect(self._open_first_nav_match)
        self._nav_search.searchSignal.connect(self._open_first_nav_match)
        outer.addWidget(self._nav_search)

        # 分组数变多后高度可能超过卡片可视区，导航自身可滚动（内容与分页共用滚动条样式）
        scroll = ScrollArea(nav)
        scroll.setWidgetResizable(True)
        install_fast_wheel(scroll)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(SystemCardFrame._scroll_style())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 2, 3, 6)
        body_layout.setSpacing(2)

        for group_title, items in self.NAV_GROUPS:
            if group_title:
                label = QLabel(group_title, body)
                label.setStyleSheet(self._nav_group_style())
                self._nav_group_labels.append(label)
                body_layout.addWidget(label)
            for tab_id, tab_name, icon_src in items:
                btn = QToolButton(body)
                btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
                btn.setIcon(self._resolve_nav_icon(icon_src))
                btn.setIconSize(QSize(18, 18))
                btn.setText(tab_name)
                btn.setFixedHeight(36)
                # 高度固定、宽度铺满导航整行：选中态色块长度不再随文字长短变化
                btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                btn.setCursor(Qt.PointingHandCursor)
                # 样式统一由父容器一份 QSS 提供（objectName + active 属性选择器）：
                # 13 个按钮逐个 setStyleSheet 会各触发一次全量 repolish（实测 24.8ms）
                btn.setObjectName("settingsNavBtn")
                btn.setProperty("active", "false")
                btn.clicked.connect(lambda _checked=False, tid=tab_id: self._set_active_page(tid))
                body_layout.addWidget(btn)
                self._nav_buttons[tab_id] = btn
                self._nav_order.append(tab_id)
        self._nav_empty = QLabel("没有匹配的设置", body)
        self._nav_empty.setAlignment(Qt.AlignCenter)
        self._nav_empty.hide()
        body_layout.addWidget(self._nav_empty)
        self._has_plugins = False
        self._nav_search.textChanged.connect(self._filter_nav)
        body_layout.addStretch(1)

        scroll.setWidget(body)
        # 内容层不透明实底（启用 Qt 滚动 blit），必须在 setWidget 之后
        make_scroll_content_opaque(body, Colors.CARD_BG.format(alpha=255), 230, Colors.CONTENT_BG)
        outer.addWidget(scroll)
        # 宽度按最长项自适应（sizeHint 已含图标/文字/内边距，只补左侧指示条与外边距）
        widest = max((btn.sizeHint().width() for btn in self._nav_buttons.values()), default=150)
        nav.setFixedWidth(max(180, min(208, widest + 20)))
        self._nav_full_width = nav.width()  # 全宽模式收起/展开动画的目标宽度
        self._nav_frame = nav
        # 供动态插件 tab 按钮插入（_sync_plugin_tabs）
        self._nav_body_layout = body_layout
        return nav

    def _filter_nav(self) -> None:
        query = self._nav_search.text().strip().casefold()
        any_match = False
        for label, (group, items) in zip(self._nav_group_labels, self.NAV_GROUPS):
            keys = [key for key, _, _ in items]
            if "plugins" in keys:
                keys.extend(self._plugin_tab_infos)
            group_match = False
            for key in keys:
                button = self._nav_buttons[key]
                terms = f"{group} {button.text()} {key} {self._NAV_KEYWORDS.get(key, '')}"
                match = query in terms.casefold() and (key != "plugins" or self._has_plugins)
                button.setVisible(match)
                group_match |= match
            label.setVisible(group_match)
            any_match |= group_match
        self._nav_empty.setVisible(not any_match)

    def _open_first_nav_match(self) -> None:
        for index in range(self._nav_body_layout.count()):
            button = self._nav_body_layout.itemAt(index).widget()
            if isinstance(button, QToolButton) and not button.isHidden():
                button.click()
                return

    @staticmethod
    def _resolve_nav_icon(icon_src) -> QIcon:
        """导航图标源 → QIcon：字符串走主题感知资源图标，AppIcon 枚举走动态主题图标"""
        if isinstance(icon_src, str):
            return get_icon(icon_src)
        # FluentIconBase.icon() 在调用瞬间把当前主题烧进静态 QIcon（文件名含颜色），
        # 主题切换后颜色不更新；qicon() 返回 FluentIconEngine 动态包装，绘制时按主题取色
        if hasattr(icon_src, "qicon"):
            return icon_src.qicon()
        return icon_src.icon() if hasattr(icon_src, "icon") else icon_src

    def _make_page(self) -> tuple:
        """创建单个分页：独立 QScrollArea + 垂直内容布局"""
        page = ScrollArea(self)
        page.setWidgetResizable(True)
        install_fast_wheel(page)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        page.setStyleSheet(SystemCardFrame._scroll_style())

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 4, 6, 4)
        layout.setSpacing(6)
        page.setWidget(inner)
        # 内容层不透明实底（启用 Qt 滚动 blit），必须在 setWidget 之后
        make_scroll_content_opaque(inner, Colors.CARD_BG.format(alpha=255), 230, Colors.CONTENT_BG)
        return page, layout

    def _set_active_page(self, tab_id: str):
        """切换左侧导航到指定分页"""
        if tab_id == self._current_tab == "plugins":
            self._apply_fullwidth_mode(tab_id)
            return
        if tab_id not in self._nav_buttons or self._current_tab == tab_id:
            return
        self._current_tab = tab_id
        self._pages_stack.setCurrentWidget(self._page_scrolls[tab_id])
        self._update_nav_styles()
        self._apply_fullwidth_mode(tab_id)
        self._expand_page_cards(tab_id)
        self.tabChanged.emit(tab_id)

    @staticmethod
    def _is_fullwidth_tab(tab_id: str) -> bool:
        """仅内置插件页独占全宽；插件注册大 tab 保持导航可见（用户反馈 2026-11-08）"""
        return tab_id == "plugins"

    def _apply_fullwidth_mode(self, tab_id: str) -> None:
        """按当前页收起/恢复左导航、顶部条与返回按钮

        收起带滑动动画；展开时若导航本来就开着由幂等守卫拦截，不重放。
        属性访问包 RuntimeError：__new__ 桩（测试 fixture）的 QObject 未初始化，
        读未定义属性抛 RuntimeError 而非 AttributeError，getattr 默认值救不了。
        """
        full = self._is_fullwidth_tab(tab_id)
        try:
            page = self.__dict__.get("pluginSettingsPage")
            if full and page is not None:
                page.set_list_visible(True)
            self._set_nav_collapsed(full, animate=full)
            back = getattr(self, "_back_btn", None)
            if back is not None:
                back.setVisible(full)
                if isinstance(back, QToolButton):
                    back.setText("显示设置导航")
        except RuntimeError:
            pass  # __new__ 桩：跳过视觉同步

    def _toggle_plugin_navigation(self):
        if self.pluginSettingsPage is not None and self.pluginSettingsPage.list_hidden():
            self._apply_fullwidth_mode("plugins")
        else:
            self._exit_fullwidth_mode()

    def _exit_fullwidth_mode(self) -> None:
        """恢复导航并保留当前插件表单。"""
        try:
            current = getattr(self, "_current_tab", None)
            self._set_nav_collapsed(False, animate=current == "plugins")
            back = getattr(self, "_back_btn", None)
            if back is not None:
                page = self.__dict__.get("pluginSettingsPage")
                if page is not None:
                    page.set_list_visible(False)
                    back.setText("选择插件")
                else:
                    back.setVisible(False)
        except RuntimeError:
            pass  # __new__ 桩：跳过视觉同步

    def _set_nav_collapsed(self, collapsed: bool, animate: bool = True) -> None:
        """左导航收起/展开：maximumWidth 滑动动画；降级或 animate=False 时直接显隐

        收起：min 置 0 后动画 max 当前宽→0，结束 hide 并把宽度恢复正常值
        （下次展开起点干净）；展开反向。连点由 stop() 旧动画防护。
        """
        try:
            nav = getattr(self, "_nav_frame", None)
            if nav is None:
                return
            full_w = getattr(self, "_nav_full_width", 0) or nav.width() or 160
            old_anim = getattr(self, "_nav_anim", None)
            if old_anim is not None:
                old_anim.stop()
            if not animate or not Animations.motion_enabled():
                nav.setVisible(not collapsed)
                return
            # 动画路径幂等守卫：目标状态已达成时不重放。普通 tab 之间切换也会
            # 走展开分支，无守卫时导航每次都被置 0 再撑开，肉眼可见闪一下
            if collapsed and not nav.isVisible():
                return
            if not collapsed and nav.isVisible() and nav.maximumWidth() >= full_w:
                return
            if collapsed:
                start = nav.width() or full_w
                nav.setMinimumWidth(0)
                nav.setMaximumWidth(start)
                anim = QPropertyAnimation(nav, b"maximumWidth", nav)
                anim.setDuration(Animations.NORMAL_MS)
                anim.setStartValue(start)
                anim.setEndValue(0)
                anim.setEasingCurve(Animations.EASE_EXIT)

                def _on_hide_done():
                    nav.setVisible(False)
                    nav.setFixedWidth(full_w)

                anim.finished.connect(_on_hide_done)
            else:
                nav.setMinimumWidth(0)
                nav.setMaximumWidth(0)
                nav.setVisible(True)
                anim = QPropertyAnimation(nav, b"maximumWidth", nav)
                anim.setDuration(Animations.NORMAL_MS)
                anim.setStartValue(0)
                anim.setEndValue(full_w)
                anim.setEasingCurve(Animations.EASE_ENTER)

                def _on_show_done():
                    nav.setFixedWidth(full_w)

                anim.finished.connect(_on_show_done)
            self._nav_anim = anim
            anim.start()
        except RuntimeError:
            pass  # __new__ 桩：跳过动画

    def _sync_plugin_nav_visibility(self) -> bool:
        """同步「插件」导航项与分区显隐：有插件就显示

        判定从「有无注册设置卡」改为「有无插件」：插件全部没有 config_schema 时
        仍要能查看它们的权限与关于，导航不能整块消失。
        """
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            has_plugins = bool(pm.is_initialized() and pm.list_plugins())
        except Exception:
            has_plugins = False
        # 容器懒建：未进插件页时 _plugin_cards_widget 为 None，只同步导航显隐
        widget = getattr(self, "_plugin_cards_widget", None)
        if widget is not None:
            widget.setVisible(has_plugins)
        try:
            self._has_plugins = has_plugins
            self._filter_nav()
        except Exception:
            pass
        return has_plugins

    def _sync_plugin_tabs(self):
        """同步插件注册的独立设置大 tab（register_settings_tab）：导航 + 分页增删

        只查注册清单不实例化页面（懒构造在 _ensure_page_ready / _build_plugin_tab_page）。
        tab_key 用 "tab:{plugin_name}:{tab_id}" 命名空间，不可能与静态 tab_id 冲突；
        新增按钮固定插在「插件」导航项之后（顺序随 priority 降序）。移除 tab 时销毁
        按钮与分页，当前停留页被移除则切回「插件」页。幂等：清单未变时零操作。
        """
        try:
            infos = self._plugin_tab_infos
        except RuntimeError:
            return  # __new__ 桩（测试 fixture）未初始化 QObject：跳过同步
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            tabs = UIPluginRegistry.get_instance().get_settings_tabs()
        except Exception:  # noqa: BLE001 - 注册表不可用时不拖垮设置页
            tabs = []
        desired = {f"tab:{t.plugin_name}:{t.tab_id}": t for t in tabs}
        if desired == infos:
            return

        # ── 移除消失的 tab ──
        removed_current = False
        for key in [k for k in infos if k not in desired]:
            infos.pop(key, None)
            self._plugin_tab_pages.pop(key, None)
            self._page_ready.discard(key)
            btn = self._nav_buttons.pop(key, None)
            # __dict__ 直取：__new__ 桩未初始化 QObject，getattr 会抛 RuntimeError
            order = self.__dict__.get("_nav_order")
            if order is not None and key in order:
                order.remove(key)
            if btn is not None:
                nav_layout = getattr(self, "_nav_body_layout", None)
                if nav_layout is not None:
                    nav_layout.removeWidget(btn)
                btn.deleteLater()
            page = self._page_scrolls.pop(key, None)
            self._page_layouts.pop(key, None)
            if page is not None:
                self._pages_stack.removeWidget(page)
                page.deleteLater()
            if self._current_tab == key:
                removed_current = True
        if removed_current:
            self._current_tab = "plugins"
            fallback = self._page_scrolls.get("plugins")
            if fallback is not None:
                self._pages_stack.setCurrentWidget(fallback)
            self._update_nav_styles()
            self._apply_fullwidth_mode("plugins")

        # ── 新增出现的 tab ──
        nav_layout = getattr(self, "_nav_body_layout", None)
        anchor = self._nav_buttons.get("plugins")
        insert_at = nav_layout.indexOf(anchor) + 1 if nav_layout is not None and anchor is not None else None
        for idx, (key, info) in enumerate(desired.items()):
            if key in infos:
                continue
            infos[key] = info
            btn = QToolButton(self._nav_frame)
            btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            btn.setIcon(self._resolve_nav_icon(info.icon if info.icon is not None else AppIcon.APPLICATION))
            btn.setIconSize(QSize(18, 18))
            btn.setText(info.title)
            btn.setFixedHeight(36)
            # 与 _build_side_nav 一致：高度固定、宽度铺满导航整行
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setObjectName("settingsNavBtn")
            btn.setProperty("active", "false")
            btn.clicked.connect(lambda _checked=False, tid=key: self._set_active_page(tid))
            self._nav_buttons[key] = btn
            # __dict__ 直取：__new__ 桩未初始化 QObject，getattr 会抛 RuntimeError
            order = self.__dict__.get("_nav_order")
            if order is not None:
                order.append(key)
            if insert_at is not None and nav_layout is not None:
                nav_layout.insertWidget(insert_at + idx, btn)
            page, page_layout = self._make_page()
            self._page_scrolls[key] = page
            self._page_layouts[key] = page_layout
            self._pages_stack.addWidget(page)
        # 导航宽度按最长项自适应（上限与 _build_side_nav 一致）
        nav_frame = getattr(self, "_nav_frame", None)
        if nav_frame is not None and self._nav_buttons:
            widest = max(b.sizeHint().width() for b in self._nav_buttons.values())
            nav_frame.setFixedWidth(max(180, min(208, widest + 20)))
            self._nav_full_width = nav_frame.width()
        if "_nav_search" in self.__dict__:
            self._filter_nav()

    def _build_plugin_tab_page(self, tab_key: str):
        """首次进入插件大 tab 时实例化页面（懒构造；失败不标 ready 由 _ensure_page_ready 重试）"""
        info = self._plugin_tab_infos.get(tab_key)
        layout = self._page_layouts.get(tab_key)
        if info is None or layout is None:
            return
        widget = info.widget_class(parent=self)
        self._plugin_tab_pages[tab_key] = widget
        layout.addWidget(widget)
        layout.addStretch(1)

    def _ensure_page_ready(self, tab_id: str):
        """分页首次进入时的一次性准备：把「只有这页才需要的重活」从打开设置挪走

        首开设置卡的成本里，非首屏页的一次性准备占了七成以上（Hooks 页的
        HookManager 注入 + 全量重渲染、工具/智能体页的组件清单首次构建、插件
        设置页的插件卡实例化）。用户可能整场会话都不点开这些页，却要在点开
        设置的那一刻同步付完 —— 这就是"点设置要卡 1~2 秒"的主体。

        改为按页延迟 + 只做一次：进入该页时才付，付完即止。
        """
        if tab_id in self._page_ready:
            return
        try:
            builder = self._LAZY_PAGE_BUILDERS.get(tab_id)
            if builder is not None:
                getattr(self, builder)()
            # 内容刷新：懒建卡在该卡存在后才能同步（tools/agents/context 首刷较重，
            # 不能放在 builder 里连同构造一超付——与既有页保持同一节奏）
            if tab_id in ("tools", "agents", "context"):
                card = getattr(
                    self,
                    {"tools": "pluginToolCard", "agents": "pluginAgentCard", "context": "pluginContextCard"}[tab_id],
                    None,
                )
                if card is not None:
                    card.refresh_components()
            elif tab_id == "plugins":
                self.rebuild_plugin_cards(force=True)
            elif tab_id == "skills":
                self._prefetch_skills()
            elif tab_id.startswith("tab:"):
                self._build_plugin_tab_page(tab_id)
            # 建完后统一对齐当前全局外观（字号/主题刷新只覆盖已存在控件）
            self._apply_appearance_to_page(tab_id)
        except Exception as e:
            # 失败不标记 ready：否则该页内容会永久为空且不再重试
            logger.warning(f"[LLMSettingsCard] {tab_id} 页首次准备失败: {e}")
            return
        self._page_ready.add(tab_id)

    def _expand_page_cards(self, tab_id: str):
        """进入分页时展开页内可展开卡片：进页即见列表，无需再点一次标题栏"""
        self._ensure_page_ready(tab_id)
        layout = self._page_layouts.get(tab_id)
        if layout is None:
            return
        for i in range(layout.count()):
            item = layout.itemAt(i)
            card = item.widget() if item is not None else None
            if card is None or not hasattr(card, "toggleExpand"):
                continue
            # 标记 _skip_auto_expand 的卡（如主题音乐歌单）默认保持收起
            if getattr(card, "_skip_auto_expand", False):
                continue
            try:
                # qfluentwidgets ExpandSettingCard 的展开状态属性是 isExpand
                if not getattr(card, "isExpand", False):
                    # ★ 同一 tick 内「刚填完内容就展开」必须先让 QScrollArea 定尺：
                    # ExpandSettingCard.setExpand(True) 内部拿
                    # `verticalScrollBar().setValue(h)` 驱动高度动画，而
                    # scrollWidget 尚未按新内容重算高度时 scrollbar.maximum 还是
                    # 旧值 → setValue 被钳制 → valueChanged 不触发 →
                    # setFixedHeight 永远不落地，表现为「列表已展开但内容看不见」。
                    # 内容填充挪到首次进页（_ensure_page_ready）后，刷新与展开
                    # 落在同一 tick，这个前置条件不再自动成立，故显式定尺。
                    inner = card.widget() if hasattr(card, "widget") else None
                    if inner is not None and inner.sizeHint().height() > inner.height():
                        inner.adjustSize()
                    card.toggleExpand()
            except Exception as e:
                logger.warning(f"[LLMSettingsCard] {tab_id} 页卡片展开失败: {e}")
        page = self._page_scrolls.get(tab_id)
        if page is not None:
            page.verticalScrollBar().setValue(0)

    def _update_nav_styles(self):
        """切换导航选中态：只动变化的那两个按钮（动态属性 + 定点 repolish）

        `_nav_qss_dirty` 由字号/字族变更置位（见 _on_config_changed）：按钮 QSS
        内嵌 font_size_css / get_font_family_css，必须重设父容器 QSS 才吃到新值。

        ⚠ 收益是可维护性，不是性能。早期记录的「逐按钮 setStyleSheet 实测
        24.8ms」是抓栈采样的失真值——该法把「在等别人」算成「自己在花」
        （本项目已三次确认：#9 信号连接 197.9ms→实际 0.1ms、#28
        scroll_bar.__initWidget 216ms→实际 0.02ms、本条 24.8ms→实际
        0.87ms）。wall-clock 复测：换色后基线逐按钮重设 0.87ms，本实现的
        父容器重设 1.57ms（反而约 +0.7ms）；同态重复调用 0.02ms vs 0.04ms。
        换来的是 QSS 只有一处定义、不必逐控件同步，不是省时间。
        """
        if self.__dict__.get("_nav_qss_dirty", False):
            self._nav_qss_dirty = False
            nav = self.__dict__.get("_nav_frame")
            if nav is not None:
                from app.utils.design_tokens import set_style_sheet_if_changed

                set_style_sheet_if_changed(nav, self._nav_frame_style())
        prev = self.__dict__.get("_styled_tab")
        cur = self.__dict__.get("_current_tab")
        if prev == cur:
            return
        self._styled_tab = cur
        if "_page_title" in self.__dict__ and cur in self._nav_buttons:
            self._page_title.setText(self._nav_buttons[cur].text())
        for tab_id in (prev, cur):
            btn = self._nav_buttons.get(tab_id) if tab_id else None
            if btn is None:
                continue
            want = "true" if tab_id == cur else "false"
            if btn.property("active") == want:
                continue
            btn.setProperty("active", want)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()

    def _nav_frame_style(self) -> str:
        """导航面板 QSS：面板底 + 导航按钮全部样式（选中态走 active 动态属性）

        按钮样式集中在一份父容器 QSS：原来每个按钮各自 setStyleSheet（Qt5 对
        同串也不短路，会全量 repolish）。⚠ 动机是可维护性不是性能：实测父容器
        重设 1.57ms 反而比逐按钮 0.87ms 略贵，省下的是「QSS 只有一处定义」，
        不是毫秒。_nav_btn_style 仍是唯一真源，字号/字族改动直接反映到这里。
        """
        inactive = self._nav_btn_style(False).replace("QToolButton", "QToolButton#settingsNavBtn")
        active = self._nav_btn_style(True).replace("QToolButton", 'QToolButton#settingsNavBtn[active="true"]')
        return f"""
            QFrame#settingsSideNav {{
                background: transparent;
                border-right: 1px solid {Colors.DIVIDER_COLOR};
            }}
            {inactive}
            {active}
        """

    @staticmethod
    def _back_btn_style() -> str:
        """全宽页顶部返回按钮：与导航项同一视觉语言（次要色，hover 提亮）"""
        return f"""
            QToolButton {{
                color: {Colors.TEXT_SECONDARY};
                {font_size_css(13)}
                padding: 0 10px;
                border: none;
                border-radius: 6px;
                background-color: transparent;
                {get_font_family_css()}
            }}
            QToolButton:hover {{
                color: {Colors.TEXT_PRIMARY};
                background-color: {Colors.TAB_HOVER_BG};
            }}
            QToolButton:focus {{
                outline: none;
            }}
        """

    @staticmethod
    def _page_title_style() -> str:
        return f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(19)} font-weight: 600;"

    @staticmethod
    def _nav_group_style() -> str:
        return f"""
            QLabel {{
                color: {Colors.TEXT_SECONDARY};
                {font_size_css(11)}
                font-weight: bold;
                padding: 12px 8px 4px;
                background: transparent;
                {get_font_family_css()}
            }}
        """

    @staticmethod
    def _nav_btn_style(active: bool) -> str:
        if active:
            return f"""
                QToolButton {{
                    color: {Colors.TEXT_PRIMARY};
                    {font_size_css(13)}
                    font-weight: bold;
                    padding: 0 10px;
                    border: none;
                    border-radius: 6px;
                    text-align: left;
                    background-color: {Colors.HOVER_BG_STRONG};
                    {get_font_family_css()}
                }}
                QToolButton:hover, QToolButton:pressed {{
                    color: {Colors.TEXT_PRIMARY};
                    background-color: {Colors.HOVER_BG_STRONG};
                }}
                QToolButton:focus {{
                    outline: none;
                }}
            """
        return f"""
            QToolButton {{
                color: {Colors.TEXT_PRIMARY};
                {font_size_css(13)}
                font-weight: normal;
                padding: 0 10px;
                border: none;
                border-radius: 6px;
                text-align: left;
                background-color: transparent;
                {get_font_family_css()}
            }}
            QToolButton:hover {{
                color: {Colors.TEXT_PRIMARY};
                background-color: {Colors.TAB_HOVER_BG};
            }}
            QToolButton:pressed {{
                color: {Colors.TEXT_PRIMARY};
                background-color: {Colors.TAB_ACTIVE_BG};
            }}
            QToolButton:focus {{
                outline: none;
            }}
        """

    @staticmethod
    def _ancestor_scroll_area(widget):
        """向上查找最近的祖先 QScrollArea（分页改造后卡片在页内滚动区中）"""
        p = widget.parentWidget()
        while p is not None:
            if isinstance(p, ScrollArea):
                return p
            p = p.parentWidget()
        return None

    def refresh_style(self):
        """主题刷新：基类样式 + 左侧导航按钮 + 页滚动条"""
        from app.utils.design_tokens import set_style_sheet_if_changed
        from app.utils.theme_refresh import ThemeRefreshCoordinator as _TRC

        _TRC.timer_start("llm_super")
        super().refresh_style()
        _TRC.timer_end("llm_super")
        _TRC.timer_start("llm_nav")
        if self._nav_frame is not None:
            set_style_sheet_if_changed(self._nav_frame, self._nav_frame_style())
        set_style_sheet_if_changed(
            self.title_label,
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(16)} font-weight: 600;",
        )
        if hasattr(self, "_nav_buttons"):
            self._update_nav_styles()
        _back_btn = getattr(self, "_back_btn", None)
        if "_page_title" in self.__dict__:
            set_style_sheet_if_changed(self._page_title, self._page_title_style())
        if _back_btn is not None:
            set_style_sheet_if_changed(_back_btn, self._back_btn_style())
        _nav_group_css = self._nav_group_style()
        for label in getattr(self, "_nav_group_labels", []):
            set_style_sheet_if_changed(label, _nav_group_css)
        for label in getattr(self, "_common_sections", []):
            set_style_sheet_if_changed(label, _nav_group_css)
        _TRC.timer_end("llm_nav")
        # [PERF] 只刷滚动条控件自身：对 QScrollArea setStyleSheet 会 repolish
        # 整页子树（每页 1400+ 控件），而主题变化只影响滚动条颜色。
        _TRC.timer_start("llm_sb")
        try:
            from app.utils.design_tokens import get_unified_scrollbar_style

            sb_style = get_unified_scrollbar_style(8)
            for page in getattr(self, "_page_scrolls", {}).values():
                for sb in (page.verticalScrollBar(), page.horizontalScrollBar()):
                    if sb is not None:
                        set_style_sheet_if_changed(sb, sb_style)
        except Exception:
            pass
        _TRC.timer_end("llm_sb")
        # 插件页是自定义行控件（非 SettingCard/SystemCardFrame），不在上面两条
        # 刷新路径的 findChildren 范围内，单独刷一次行样式（选中/hover 用主题色）
        try:
            page = getattr(self, "pluginSettingsPage", None)
            if page is not None:
                page.refresh_style()
        except Exception:
            pass

    def _setup_appearance_cards(self):
        self.uiFontSizeCard = FontSizeStepperCard(
            get_icon("字体大小"),
            "界面字号",
            "统一调整界面与对话内容字号",
            self.cfg,
            self,
        )
        self.uiChatWidthCard = OptionsSettingCard(
            self.cfg.ui_chat_width,
            AppIcon.LAYOUT,
            "对话区宽度",
            "窗口过宽时限制内容宽度并居中",
            texts=["窄", "标准", "宽", "不限宽"],
            parent=self,
        )
        # 深浅是三态（自动 / 深 / 浅），布尔开关表达不了「跟随系统」，
        # 换自绘分段控件（见 theme_mode_card 模块）
        self.uiThemeModeCard = ThemeModeSettingCard(self.cfg.ui_theme_mode, self)
        self.uiThemeStyleCard = self._make_theme_style_card()

    def _make_theme_style_card(self):
        """构建主题风格卡（网格预览选择器，见 theme_style_card 模块）

        条目采集与 validator 对齐统一走 prepare_theme_entries；主题源变化时
        卡片自己监听 theme_manager.on_reload 重建网格。
        """
        is_light = bool(self.cfg.ui_light_mode.value)
        entries = prepare_theme_entries(self.cfg.ui_theme_style, self.cfg, is_light)
        if not entries:
            return getattr(self, "uiThemeStyleCard", None)
        return ThemeStyleCard(
            self.cfg.ui_theme_style,
            entries,
            self.cfg,
            is_light=is_light,
            icon=get_icon("主题风格"),
            parent=self,
        )

    def _on_motion_override_changed(self, checked: bool) -> None:
        """动效覆盖切换 → 丢弃系统偏好探测缓存

        配置值本身实时生效，清缓存是为了让「关闭覆盖」时重新读一次系统
        开关（用户可能在运行期动过系统设置），而不是沿用启动时的旧值。
        """
        Animations.refresh_reduce_motion()

    def _on_music_volume_changed(self, value: int) -> None:
        """主题音乐音量滑块 → 播放器（value 为 0~100）"""
        try:
            from app.widgets.theme_music_player import ThemeMusicPlayer

            ThemeMusicPlayer.instance().set_volume(value / 100.0)
        except Exception:
            pass

    def _rebuild_theme_style_card(self) -> None:
        """按当前深浅模式重建主题风格卡（选项动态过滤），保持原布局位置"""
        if "appearance" not in self._page_ready:
            # 外观页未构造时无需重建：builder 构建时按当前配置取主题集（响应式）
            return
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

    def _setup_font_card(self):
        """创建字体设置卡片"""
        from qfluentwidgets import SettingCard

        class FontSettingCard(SettingCard):
            def __init__(self, title, content, item, parent):
                super().__init__(AppIcon.FONT, title, content, parent)
                self.cfg = parent.cfg
                self.item = item

                self.fontCombo = NoWheelFontComboBox()
                self.fontCombo.setFixedWidth(180)
                self.fontCombo.setSizeAdjustPolicy(QFontComboBox.SizeAdjustPolicy.AdjustToContents)
                self._apply_font_combo_style()
                self.fontCombo.currentFontChanged.connect(self._on_font_changed)

                self.hBoxLayout.addWidget(self.fontCombo)
                self.hBoxLayout.addSpacing(16)
                item.valueChanged.connect(self._sync_font)
                self._sync_font()

            def _sync_font(self):
                self.fontCombo.blockSignals(True)
                try:
                    self.fontCombo.setCurrentFont(QFont(self.item.value or system_font_family()))
                finally:
                    self.fontCombo.blockSignals(False)

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
                self.cfg.set(self.item, font.family(), save=True)

        self.uiFontCard = FontSettingCard("界面字体", "用于导航、菜单和设置控件。", self.cfg.ui_font_family, self)
        self.llmFontCard = FontSettingCard(
            "对话字体",
            "用于提问和回答的正文。",
            self.cfg.llm_font_family,
            self,
        )

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def _on_light_mode_changed(self, is_light: bool):
        """生效深浅变化（模式切档 / 自动跟随系统 / 外部同步）

        主题与生效深浅的对齐收在 app.utils.theme_mode.apply_light：只在当前
        主题深浅与目标不符时才切默认主题，不再无条件把用户手选的主题打回默认。
        """
        from app.utils.theme_mode import is_sync_applying

        if is_sync_applying():
            # 云端配置写回循环中途的衍生信号：此刻 ThemeStyle/ThemeMode 可能
            # 还是旧值，此处插手会 apply_light(save=True) 把半同步态落盘成
            # 「浅色模式+深色主题」矛盾文件（曾致同步后主题被闪回本地旧主题）。
            # 深浅对齐由 _reload_settings_on_main_thread 末尾的统一 apply_light 兜底。
            return
        apply_light(is_light)
        # 重建主题风格卡（选项按新模式过滤）
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
        elif sender in (self.cfg.ui_light_mode, self.cfg.ui_theme_mode):
            # 深浅 / 外观模式切档 → 实质是换主题
            LLMSettingsCard._last_change_type = "theme"
        elif sender in (self.cfg.llm_font_family, self.cfg.ui_font_family):
            LLMSettingsCard._last_change_type = "font_family"
        elif sender is self.cfg.ui_font_size:
            LLMSettingsCard._last_change_type = "font_size"
        elif sender is self.cfg.ui_chat_width:
            # 只改对话区限宽，不涉及字体/主题缓存失效
            LLMSettingsCard._last_change_type = "chat_width"
        else:
            LLMSettingsCard._last_change_type = None  # 未知类型，全量刷新

        # 局部缓存判断标志：emit 后 OpenAIChatToolWindow._on_settings_config_changed
        # 会同步把 _last_change_type 清零（批量刷新用），那时再读就拿不到了
        needs_nav_rebuild = LLMSettingsCard._last_change_type in ("font_size", "font_family")
        self.configChanged.emit()
        # 所有配置控件在变更时已即时保存，这里只负责刷新运行时外观

        # 字体/字号变更 → 即时重建左侧导航按钮 QSS
        # （按钮样式串内嵌 font_size_css/get_font_family_css，旧 QSS 会压制
        #   apply_font_size_to_widget 的 setFont；不重建则切 tab 前字体不生效）
        if needs_nav_rebuild:
            try:
                # 按钮 QSS 唯一真源在父容器（见 _nav_frame_style）：置脏后由
                # _update_nav_styles 重设一次，字号/字族才能即时生效
                self._nav_qss_dirty = True
                self._update_nav_styles()
            except Exception:
                pass
            # 插件设置页字号跟随：标签/表单 QSS 烧死字号，靠切插件重建才吃到
            # 新值的旧路径覆盖不了已显示的详情页，这里显式刷
            try:
                page = getattr(self, "pluginSettingsPage", None)
                if page is not None:
                    page.refresh_style()
            except RuntimeError:
                pass  # __new__ 桩（测试 fixture）无真实页面

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
        for card_name in (
            "uiFontSizeCard",
            "uiChatWidthCard",
            "uiThemeModeCard",
            "uiThemeStyleCard",
            "uiMusicCard",
            "uiFontCard",
            "llmFontCard",
            "renderRestartCard",
            "renderStatusCard",
        ):
            card = getattr(self, card_name, None)
            if card is not None and hasattr(card, "refresh_style"):
                card.refresh_style()
        # 手风琴类卡片（ExpandSettingCard 子类，不在以上遍历范围）
        for card_name in (
            "llmSkillsCard",
            "llmProviderCard",
            "hookListCard",
            "mcpListCard",
            "lspListCard",
            "pluginToolCard",
            "pluginAgentCard",
            "renderAdvancedCard",
            "updateProxyCard",
            "secretModeCard",
        ):
            card = getattr(self, card_name, None)
            if card is not None and hasattr(card, "refresh_style"):
                card.refresh_style()
        # 安全中心：容器级 QSS 构造时定格，需显式重设（页懒构建，未建时 findChildren 为空即构造时已用新主题）
        from app.widgets.cards.settings.security_center_card import SecurityCenterCard

        for card in self.findChildren(SecurityCenterCard):
            if hasattr(card, "refresh_style"):
                try:
                    card.refresh_style()
                except RuntimeError:
                    pass
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

            # 注册表现状短路：目标状态与本机注册表一致时无需提权重写，
            # 兼防同步/配置回写等程序化联动误触发 UAC 弹窗
            reg_cmd = get_registered_command()
            if enabled and reg_cmd == build_startup_command():
                logger.info("[AutoStart] 注册表已开启且命令有效，跳过重复写入")
                return
            if not enabled and reg_cmd is None:
                logger.info("[AutoStart] 注册表本就无自启项，跳过删除")
                return

            # 1. 先弹 UAC 由提权 helper 写 HKLM 注册表（独立 try，不相互污染异常处理）
            try:
                request_auto_start_update(enabled)
            except AutoStartCancelled:
                # 用户在 UAC 弹窗点"否"：静默回退开关，不打扰
                self.autoStartCard.switchButton.setChecked(not enabled)
                self.cfg.set(self.cfg.auto_start, not enabled, save=True)
                return
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
        from app.core.local_service import (
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
        from app.core.local_service import (
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
        # [T22] 隐藏期主题切换补刷：main_widget 5a 门控把不可见期的主题刷新
        # 置脏跳过（0.7s 级重型卡白刷），显示时在此自愈。scope 含 font 时门控
        # 不生效（标记恒 False），此分支空转无害。
        if getattr(self, "_theme_needs_refresh", False):
            self._theme_needs_refresh = False
            self._refresh_appearance_from_config()
        if hasattr(self, "llmProviderCard"):
            self.llmProviderCard._refresh_items()
        # 订阅热重载广播（放这里而非 __init__：避免过早拉起 PluginHostService，
        # 后者会连带全量加载智能体 + 启动文件监听，实测约 330ms）
        self._ensure_hot_reload_connected()
        # 重新打开设置卡时恢复正常导航（全宽模式不跨开关保留，分页不动）
        self._exit_fullwidth_mode()
        # 预热退场：原实现在 2.05s 内把 10 页全量建完（实测 +1105 控件、267ms
        # 构建），且无取消机制——用户关掉设置后 singleShot 照建不误。改为
        # 「停留够久才预热当前页 + 相邻页」，其余页首次进页才付（_ensure_page_ready）。
        self._schedule_idle_prefetch()
        # 打开设置时刷新工具/智能体列表（插件热重载/启停后保持最新）。
        # ★ 仅限「已访问过的页」：未访问页的第一次 refresh_components 会走全量
        # 构建（数百毫秒），那笔由 _ensure_page_ready 在该页首次进入时付，
        # 不在这里替用户垫付。
        for tab_id, card_name in (("tools", "pluginToolCard"), ("agents", "pluginAgentCard")):
            if tab_id not in self._page_ready:
                continue
            card = getattr(self, card_name, None)
            if card is not None:
                card.refresh_components()
        super().showEvent(event)

    # 停留多久才预热相邻页：低于此值说明用户只是扫一眼首屏就关，预热是纯浪费
    _IDLE_PREFETCH_MS = 1500
    # 相邻页之间的错峰间隔：单帧只付一页的钱（冷缓存单页可达 60ms）
    _PREFETCH_STEP_MS = 150

    # 重模块预 import 清单（#41）：两页首进成本的 95%+ 是 Python 首次 import，
    # 实测冷热差 mcp 137ms / lsp 141ms（mcp_setting_card 单独 146.8ms，其中 mcp
    # SDK 本体 64.9ms；lsp_manager 单独 141.8ms）。import 阶段实测新增线程 0，
    # 无 event loop / 子进程副作用，故可安全地挪到空闲线程。
    _HEAVY_PREWARM_MODULES = (
        "app.widgets.cards.settings.mcp_setting_card",
        "app.core.lsp.lsp_manager",
    )
    _heavy_prewarm_started: bool = False  # __new__ 桩兜底

    def _prewarm_heavy_modules(self):
        """空闲时在后台线程预 import mcp / lsp 两页的重模块（幂等，失败静默）

        为什么放线程：单模块 import 141~147ms 且持 GIL，放主线程等于把懒建的
        收益吐回去（设置卡构造 41ms 会涨到 ~180ms）。线程里做只影响 GIL 争用，
        且此时用户已停留 1.5s、正在看首屏，交互密度低。

        只 import 不初始化：不建 Qt 对象、不调 get_lsp_manager()、不探测服务器，
        所以不涉及 Qt 线程亲和性。失败静默（DEBUG）：用户点进该页时按正常路径
        再 import 一次，行为与未预热的现状完全一致——退化安全。
        """
        if self.__dict__.get("_heavy_prewarm_started", False):
            return
        try:
            self._heavy_prewarm_started = True
        except RuntimeError:
            return  # __new__ 桩：无 QObject，也就不需要预热

        def _run():
            import importlib

            for mod in self._HEAVY_PREWARM_MODULES:
                try:
                    importlib.import_module(mod)
                except Exception as e:  # noqa: BLE001 - 预热失败绝不能影响使用
                    logger.debug(f"[LLMSettingsCard] 预 import {mod} 失败（退化为按需加载）: {e}")

        try:
            import threading

            threading.Thread(target=_run, name="settings-prewarm", daemon=True).start()
        except Exception as e:  # noqa: BLE001 - 起线程失败同样只降级
            logger.debug(f"[LLMSettingsCard] 预 import 线程启动失败（退化为按需加载）: {e}")

    def _schedule_idle_prefetch(self):
        """启动空闲预热定时器（停留 1.5s 且窗口仍可见才触发，见 _prefetch_pages）"""
        try:
            timer = getattr(self, "_idle_prefetch_timer", None)
            if timer is None:
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.setInterval(self._IDLE_PREFETCH_MS)
                timer.timeout.connect(self._prefetch_pages)
                self._idle_prefetch_timer = timer
            timer.start()
        except RuntimeError:
            pass  # __new__ 桩：无 QObject

    def _prefetch_pages(self):
        """停留够久后预热「当前页 + 前后相邻页」，不碰其余页

        为什么不再全量预热：原实现把 10 页排进 singleShot（`250 + i*200`，2.05s
        内逐页建完，+1105 控件、267ms 构建），用户可能只是看一眼首屏就关，
        白付全部成本且无取消机制。首屏可见时间因此从 ~218ms 涨到 1.26s。

        为什么仍保留少量预热：相邻页是用户最可能点的下一页，停留超 1.5s 说明
        八成在找东西，提前把邻居内容备好，点击时只需付展开动画。
        """
        if not self.isVisible():
            return
        # [PERF #41] 重模块空闲预 import：mcp/lsp 两页首进成本 95%+ 是 Python 首次
        # import（冷热差 137/141ms）。用户在设置页停留超 1.5s 后才走到这里，正是
        # 把这两笔挪去后台线程的窗口；放本方法而非 _schedule_idle_prefetch，是因为
        # 已有 isVisible 守卫（不可见时不起线程）。
        self._prewarm_heavy_modules()
        order = self.__dict__.get("_nav_order") or ()
        cur = self.__dict__.get("_current_tab")
        if cur not in order:
            return
        i = order.index(cur)
        targets = [order[j] for j in (i, i + 1, i - 1) if 0 <= j < len(order)]
        self._prefetch_queue = [t for t in dict.fromkeys(targets) if t not in self._page_ready]
        self._prefetch_tick()

    def _prefetch_tick(self):
        """逐页错峰预热：一次只建一页，间隔 _PREFETCH_STEP_MS 再建下一页"""
        queue = self.__dict__.get("_prefetch_queue") or []
        if not queue or not self.isVisible():
            self._prefetch_queue = []
            return
        tab_id = queue.pop(0)
        if tab_id not in self._page_ready:
            try:
                self._ensure_page_ready(tab_id)
            except RuntimeError:
                self._prefetch_queue = []
                return
        if not queue:
            return
        try:
            timer = getattr(self, "_prefetch_step_timer", None)
            if timer is None:
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.setInterval(self._PREFETCH_STEP_MS)
                timer.timeout.connect(self._prefetch_tick)
                self._prefetch_step_timer = timer
            timer.start()
        except RuntimeError:
            pass  # __new__ 桩：无 QObject

    def set_opacity(self, opacity: float):
        """设置透明度（保留接口，暂不实现动态透明度）"""
        pass
