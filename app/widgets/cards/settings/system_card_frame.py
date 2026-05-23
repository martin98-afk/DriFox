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
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame, QLineEdit,
)
from qfluentwidgets import (
    StrongBodyLabel, TransparentToolButton, FluentIcon, PrimaryToolButton)

from app.utils.design_tokens import Colors, TabStyles, font_size_css
from app.utils.utils import get_unified_font, get_icon, get_font_family_css


class SystemCardFrame(QFrame):
    """系统卡片基类 — 固定边框样式，无动画"""

    closed = pyqtSignal()
    tabChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_base_ui()

    # ── UI 构建 ──────────────────────────────────────────

    def _build_base_ui(self):
        self.setSizePolicy(1, 0)
        self.setFixedHeight(180)
        self._apply_base_style()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 5, 6, 5)
        main_layout.setSpacing(4)

        # ── 头部 ──
        self._header_layout = QHBoxLayout()
        self._header_layout.setSpacing(4)

        self.icon_label = QLabel(self)
        self.icon_label.setFont(get_unified_font(12))

        self.title_label = StrongBodyLabel(self)
        self.title_label.setFont(get_unified_font(12, True))
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

        # 模式切换按钮容器（如 JSON/表单）
        self._mode_buttons_container = QHBoxLayout()
        self._mode_buttons_container.setSpacing(4)
        self._header_layout.addLayout(self._mode_buttons_container)

        # 标签按钮容器
        self._tab_buttons_container = QHBoxLayout()
        self._tab_buttons_container.setSpacing(1)
        self._header_layout.addLayout(self._tab_buttons_container)

        self._header_layout.addStretch()

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
        self.icon_label.setFont(get_unified_font(12))
        self._count_label.setFont(get_unified_font(10))
        self._count_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; padding-left: 2px;")
        self.scroll_area.setStyleSheet(self._scroll_style())
        if hasattr(self, "_tab_buttons"):
            self._update_tab_styles()

    @staticmethod
    def _scroll_style() -> str:
        return """
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollArea > QWidget > QWidget {
                background: transparent;
            }
            QScrollBar:vertical {
                border: none;
                background: transparent;
                width: 10px;
                margin: 4px 2px 4px 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.18);
                border-radius: 5px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255,255,255,0.3);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
        """

    # ── 公开控制 ───────────────────────────────────────

    def set_icon(self, icon: str):
        self.icon_label.setText(icon)

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
        self._search_input.setMaximumWidth(160)
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
                color: rgba(255,255,255,0.35);
            }}
        """)
        self._search_input.textChanged.connect(callback)
        self._search_input.setVisible(False)
        self._search_container.addWidget(self._search_input)

    def set_count_label(self, text: str):
        self._count_label.setText(f"({text})" if text else "")
        self._count_label.setVisible(bool(text))

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

    def set_extra_button_handler(self, handler):
        while self._extra_buttons_container.count():
            item = self._extra_buttons_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        btn = TransparentToolButton(get_icon("导入"), self)
        btn.setToolTip("导入会话")
        btn.clicked.connect(handler)
        self._extra_buttons_container.addWidget(btn)

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

    def set_opacity(self, opacity: float):
        pass
