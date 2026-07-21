# -*- coding: utf-8 -*-
"""
SystemCardFrame — QFrame 基类 + 标准头部布局 + 固定边框

用于所有系统设置卡片（settings/history/memory/model_config/provider_edit/hook_edit 等）
- 固定边框（无动画）
- 标准头部（图标 + 标题 + 标签/统计 + 关闭按钮）
- ScrollArea 内容区
"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, PrimaryToolButton, StrongBodyLabel, TransparentToolButton

from app.utils.design_tokens import Colors, TabStyles, font_size_css, get_unified_scrollbar_style, scale_icon_size
from app.utils.utils import get_font_family_css, get_icon, get_unified_font


class SystemCardFrame(QFrame):
    """系统卡片基类 — 固定边框样式，无动画"""

    closed = pyqtSignal()
    tabChanged = pyqtSignal(str)

    # 高度模式：'proportional' = 随窗口缩放（默认），'content' = 按内容自适应
    _height_mode: str = "proportional"

    def __init__(self, parent=None):
        super().__init__(parent)
        # 构造期间即隐藏：系统卡片内容重（子卡片多），若构造时父窗口已可见
        # （如懒加载场景），可见但未布局定位的卡片会瞬间绘制在 (0,0) 造成残影。
        # 所有系统卡片均经 CardManager 切换显隐，默认隐藏不影响正常使用。
        self.setVisible(False)
        self._height_mode = SystemCardFrame._height_mode
        self._build_base_ui()

    def set_height_mode(self, mode: str):
        """设置高度模式

        'proportional': sizeHint 返回窗口高度的 85%（默认，适合有 ScrollArea 的卡片）
        'content':      sizeHint 返回内容自然高度（适合编辑器/配置表单等需完整展示的卡片）
        """
        if mode not in ("proportional", "content"):
            return
        self._height_mode = mode
        self.updateGeometry()

    # ── UI 构建 ──────────────────────────────────────────

    # ── UI 构建 ──────────────────────────────────────────

    def _build_base_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._apply_base_style()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 5, 6, 5)
        main_layout.setSpacing(4)

        # ── 头部 ──
        self._header_layout = QHBoxLayout()
        self._header_layout.setSpacing(4)

        self.icon_label = QLabel(self)
        self.icon_label.setFont(get_unified_font(11))

        self.title_label = StrongBodyLabel(self)
        self.title_label.setFont(get_unified_font(11, True))
        Colors.refresh()
        self.title_label.setStyleSheet(f"color: {Colors.TEXT_ACCENT};")

        self._header_layout.addWidget(self.icon_label)
        self._header_layout.addWidget(self.title_label)

        # 数量统计
        self._count_label = QLabel("", self)
        self._count_label.setFont(get_unified_font(10))
        self._count_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; padding-left: 2px;")
        self._count_label.setVisible(False)
        self._header_layout.addWidget(self._count_label)

        # 标题栏标签（如吸顶服务商名称，默认隐藏）
        self._header_sticky_label = QLabel("", self)
        self._header_sticky_label.setVisible(False)
        self._header_layout.addWidget(self._header_sticky_label)

        # 模式切换按钮容器（如 JSON/表单）
        self._mode_buttons_container = QHBoxLayout()
        self._mode_buttons_container.setSpacing(4)
        self._header_layout.addLayout(self._mode_buttons_container)

        # 标签按钮容器
        self._tab_buttons_container = QHBoxLayout()
        self._tab_buttons_container.setSpacing(1)
        self._header_layout.addStretch()
        self._header_layout.addLayout(self._tab_buttons_container)

        # 搜索框容器（默认隐藏）
        self._search_container = QHBoxLayout()
        self._search_container.setSpacing(0)
        self._header_layout.addLayout(self._search_container)

        # 额外按钮容器
        self._extra_buttons_container = QHBoxLayout()
        self._extra_buttons_container.setSpacing(4)
        self._header_layout.addLayout(self._extra_buttons_container)

        # 关闭按钮
        self.close_btn = TransparentToolButton(FluentIcon.CLOSE)
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.mousePressEvent = lambda e: self._on_close()
        self._header_layout.addWidget(self.close_btn)

        main_layout.addLayout(self._header_layout)

        # ── 内容区 ──
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet(self._scroll_style())
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self.content_widget)
        self._content_layout.setContentsMargins(4, 2, 4, 2)
        self._content_layout.setSpacing(4)

        self.scroll_area.setWidget(self.content_widget)
        main_layout.addWidget(self.scroll_area, 1)

    @property
    def content_layout(self):
        return self._content_layout

    # ── 样式 ──────────────────────────────────────────

    def _apply_base_style(self):
        Colors.refresh()
        self.setStyleSheet(f"""
            SystemCardFrame {{
                background: {Colors.CARD_BG.format(alpha=230)};
                border: 1px solid {Colors.BORDER};
                border-radius: 10px;
            }}
        """)

    def refresh_style(self):
        Colors.refresh()
        self._apply_base_style()
        self.title_label.setFont(get_unified_font(12, True))
        self.title_label.setStyleSheet(f"color: {Colors.TEXT_ACCENT};")
        if self.icon_label is not None:
            self.icon_label.setFont(get_unified_font(12))
        # 头部自定义图标 widget 同步缩放
        icon_widget = getattr(self, "_icon_widget", None)
        if icon_widget is not None:
            base_size = getattr(self, "_icon_base_size", 20)
            s = scale_icon_size(base_size)
            icon_widget.setFixedSize(s, s)
        self._count_label.setFont(get_unified_font(10))
        self._count_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; padding-left: 2px;")
        self.scroll_area.setStyleSheet(self._scroll_style())
        # 强制滚动条重新应用样式表：unpolish/polish 让 Qt 重新评估 QSS
        # 深色/浅色切换后旧样式可能被缓存，unpolish+polish 迫使 Qt 重新解析颜色
        sb_vertical = self.scroll_area.verticalScrollBar()
        if sb_vertical is not None:
            sb_style = sb_vertical.style()
            if sb_style is not None:
                sb_style.unpolish(sb_vertical)
                sb_style.polish(sb_vertical)
        sb_horizontal = self.scroll_area.horizontalScrollBar()
        if sb_horizontal is not None:
            sb_style = sb_horizontal.style()
            if sb_style is not None:
                sb_style.unpolish(sb_horizontal)
                sb_style.polish(sb_horizontal)
        if hasattr(self, "_tab_buttons"):
            self._update_tab_styles()
        # 刷新搜索框样式（如果存在）
        if hasattr(self, "_search_input") and self._search_input is not None:
            self._search_input.setStyleSheet(f"""
                QLineEdit {{
                    background: {Colors.HOVER_BG};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 4px;
                    color: {Colors.TEXT_PRIMARY};
                    padding: 2px 8px;
                    {font_size_css(11)}
                    {get_font_family_css()}
                }}
                QLineEdit:focus {{
                    border: 1px solid {Colors.TEXT_ACCENT};
                }}
                QLineEdit::placeholder {{
                    color: {Colors.INPUT_PLACEHOLDER};
                }}
            """)
        # 刷新头部粘性标签样式（如果存在）
        if hasattr(self, "_header_sticky_label") and self._header_sticky_label.isVisible():
            self._header_sticky_label.setStyleSheet(f"""
                color: {Colors.ACCENT_WARM};
                {font_size_css(11)}
                padding: 0 2px 0 6px;
                font-weight: bold;
            """)
        # 内容区子控件递归刷新（如 HistoryCard/MemoryCardContent 等）
        self._refresh_content_children()

    def _refresh_content_children(self):
        """递归刷新内容区子控件的主题样式"""
        for i in range(self._content_layout.count()):
            item = self._content_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if hasattr(w, "refresh_style"):
                    try:
                        w.refresh_style()
                    except Exception:
                        pass

    @staticmethod
    def _scroll_style() -> str:
        return (
            """
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollArea > QWidget > QWidget {
                background: transparent;
            }
            """
            + get_unified_scrollbar_style(8)
            + """
        """
        )

    # ── 公开控制 ───────────────────────────────────────

    def set_icon(self, icon: str):
        if self.icon_label is not None:
            self.icon_label.setText(icon)

    def set_icon_widget(self, widget):
        """用自定义 widget 替换头部文字图标（如 ProviderIconWidget）

        图标大小随系统字体大小自适应。默认基准 20px，按 FONT_SIZE_OPTIONS.delta 缩放。
        子类如需使用其他基准尺寸，可在 set_icon_widget 之后手动 setFixedSize。
        """
        old = getattr(self, "_icon_widget", None)
        if old is not None:
            self._header_layout.replaceWidget(old, widget)
            old.deleteLater()
        else:
            idx = self._header_layout.indexOf(self.icon_label)
            if idx >= 0:
                self._header_layout.replaceWidget(self.icon_label, widget)
                self.icon_label.deleteLater()
                self.icon_label = None
        self._icon_widget = widget
        self._icon_base_size = 20
        s = scale_icon_size(self._icon_base_size)
        widget.setFixedSize(s, s)

    def set_title_text(self, text: str):
        self.title_label.setText(text)

    def set_count(self, count: int, limit: int = None):
        if limit and limit > 0:
            self._count_label.setText(f"({count}/{limit})")
        elif count > 0:
            self._count_label.setText(f"({count})")
        else:
            self._count_label.setText("")
        self._count_label.setVisible(count > 0 or (limit and limit > 0))

    def set_search_handler(self, placeholder: str, callback):
        """设置头部搜索框（在标签按钮右侧、额外按钮左侧）
        placeholder: 占位文本
        callback(text): 文本变化回调
        """
        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText(placeholder)
        self._search_input.setMaximumWidth(200)
        self._search_input.setMinimumWidth(100)
        self._search_input.setFixedHeight(24)
        Colors.refresh()
        self._search_input.setStyleSheet(f"""
            QLineEdit {{
                background: {Colors.HOVER_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                padding: 2px 8px;
                {font_size_css(11)}
                {get_font_family_css()}
            }}
            QLineEdit:focus {{
                border: 1px solid {Colors.TEXT_ACCENT};
            }}
            QLineEdit::placeholder {{
                color: {Colors.INPUT_PLACEHOLDER};
            }}
        """)
        self._search_input.textChanged.connect(callback)
        self._search_container.addWidget(self._search_input)

    def set_count_label(self, text: str):
        self._count_label.setText(f"({text})" if text else "")
        self._count_label.setVisible(bool(text))

    def set_header_sticky(self, text: str):
        """在标题栏显示标签（如吸顶服务商名称），置于标题和搜索框之间"""
        if text:
            Colors.refresh()
            self._header_sticky_label.setText(f"❮{text}❯")
            self._header_sticky_label.setStyleSheet(f"""
                color: {Colors.ACCENT_WARM};
                {font_size_css(11)}
                padding: 0 2px 0 6px;
                font-weight: bold;
            """)
            self._header_sticky_label.setVisible(True)
        else:
            self._header_sticky_label.setVisible(False)
            self._header_sticky_label.setText("")

    def setup_tabs(self, tabs: list, default_tab: str = None):
        while self._tab_buttons_container.count():
            item = self._tab_buttons_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._tabs = tabs
        self._default_tab = default_tab or (tabs[0][0] if tabs else None)
        self._current_tab = self._default_tab
        self._tab_buttons = {}

        for tab_id, tab_name in tabs:
            btn = QLabel(f"{tab_name}", self)
            btn.setFont(get_unified_font(12))
            btn.setStyleSheet(TabStyles.inactive())
            btn.setCursor(Qt.PointingHandCursor)
            btn.mousePressEvent = lambda e, tid=tab_id: self._on_tab_clicked(tid)
            self._tab_buttons_container.addWidget(btn)
            self._tab_buttons[tab_id] = btn

        self._update_tab_styles()

    def _on_tab_clicked(self, tab_id: str):
        if self._current_tab != tab_id:
            self._set_current_tab(tab_id)

    def set_current_tab(self, tab_id: str):
        """程序化切换当前标签（同时更新头部按钮状态并触发信号）"""
        if tab_id in self._tab_buttons and self._current_tab != tab_id:
            self._set_current_tab(tab_id)

    def _set_current_tab(self, tab_id: str):
        self._current_tab = tab_id
        self._update_tab_styles()
        self.tabChanged.emit(tab_id)

    def _update_tab_styles(self):
        for tab_id, btn in self._tab_buttons.items():
            btn.setStyleSheet(TabStyles.active() if tab_id == self._current_tab else TabStyles.inactive())
            btn.setFont(get_unified_font(12))

    def set_extra_button_handler(self, handler, icon=None, tooltip=""):
        """设置标题栏额外按钮（通用方法）

        Args:
            handler: 按钮点击回调
            icon: 图标（FluentIcon 或 QIcon，默认"导入"图标）
            tooltip: 悬浮提示文字
        """
        while self._extra_buttons_container.count():
            item = self._extra_buttons_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if icon is None:
            icon = get_icon("导入")
        btn = TransparentToolButton(icon, self)
        btn.setFixedSize(26, 26)
        btn.setToolTip(tooltip or "导入会话")
        btn.clicked.connect(handler)
        self._extra_buttons_container.addWidget(btn)

    def add_header_button(self, icon, tooltip: str, callback) -> TransparentToolButton:
        """向标题栏右侧添加自定义操作按钮

        Args:
            icon: FluentIcon 或 QIcon 图标
            tooltip: 按钮悬浮提示
            callback: 点击回调函数

        Returns:
            TransparentToolButton: 创建的按钮对象
        """
        btn = TransparentToolButton(icon, self)
        btn.setFixedSize(28, 28)
        btn.setToolTip(tooltip)
        btn.clicked.connect(callback)
        self._extra_buttons_container.addWidget(btn)
        return btn

    def set_mode_buttons(self, buttons: list):
        """
        设置头部模式切换按钮列表。
        buttons: [{"label": "显示文本", "active": bool, "handler": callable}, ...]
        """
        while self._mode_buttons_container.count():
            item = self._mode_buttons_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for i, btn_data in enumerate(buttons):
            btn = QLabel(btn_data["label"], self)
            btn.setCursor(Qt.PointingHandCursor)
            active = btn_data.get("active", False)
            btn.setStyleSheet(TabStyles.active() if active else TabStyles.inactive())
            btn.mousePressEvent = lambda e, h=btn_data["handler"]: h()
            self._mode_buttons_container.addWidget(btn)

    def set_save_button_handler(self, handler):
        while self._extra_buttons_container.count():
            item = self._extra_buttons_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        btn = PrimaryToolButton(FluentIcon.SAVE, self)
        btn.setFixedSize(30, 30)
        btn.clicked.connect(handler)
        self._extra_buttons_container.addWidget(btn)

    # ── 生命周期 ──────────────────────────────────────

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def show(self):
        self.setVisible(True)
        self.raise_()

    def hide(self):
        self.setVisible(False)

    def sizeHint(self):
        """根据高度模式返回卡片期望高度

        'proportional': 窗口高度的 85%（默认）
        'content':      内容自然高度（直接计算 header + 内容区实际高度，
                        绕过 QScrollArea.sizeHint() 传播问题）

        CardContainer._do_expand() 读取此值进行展开动画。
        """
        from PyQt5.QtCore import QSize

        base = super().sizeHint()
        w = max(base.width(), 200)
        # content 模式：直接计算 header + 内容区实际高度
        if self._height_mode == "content":
            self.layout().invalidate()
            self.layout().activate()
            # header 高度 + 边距 + spacing
            h = self._header_layout.sizeHint().height()
            h += self.layout().contentsMargins().top() + self.layout().contentsMargins().bottom()
            h += self.layout().spacing()
            # scroll_area 内部内容实际高度
            if self.scroll_area and self.scroll_area.widget():
                cw = self.scroll_area.widget()
                if cw.layout():
                    content_h = cw.layout().sizeHint().height()
                    h += content_h + self.scroll_area.frameWidth() * 2
            return QSize(w, max(h, self.minimumHeight()))
        # proportional 模式：按窗口比例缩放
        win = self.window()
        if win and win.height() > 0:
            target_h = max(self.minimumHeight(), int(win.height() * 0.85))
            return QSize(w, target_h)
        return base

    def showEvent(self, event):
        """显示时安装窗口 resize 事件过滤器，窗口缩放时通知容器重新展开"""
        super().showEvent(event)
        win = self.window()
        if win:
            win.installEventFilter(self)
            self.updateGeometry()

    def eventFilter(self, obj, event):
        """监听窗口 resize，触发 updateGeometry → CardContainer 重算高度"""
        from PyQt5.QtCore import QEvent

        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)

    def set_opacity(self, opacity: float):
        pass
