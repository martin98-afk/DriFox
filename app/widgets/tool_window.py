# -*- coding: utf-8 -*-
"""工具窗口壳三件套 — 自 main_widget.py 迁出（2026-09-29 拆分步骤 2）

- ``ToolWindowTitleBar``：无边框工具窗口标题栏（原 app/tool_popup.py 定义，
  随 ToolPopupDialog 下线先迁 main_widget，再迁至此）
- ``ToolWindow``：工具窗口基类，``OpenAIChatToolWindow`` 的直接基类
- ``_ToolReloadNoticeBridge`` / ``_tool_reload_notice_bridge``：工具热重载
  风险通知桥（watcher 后台线程 emit → 主线程槽）

所有权说明：模块级单例 ``_tool_reload_notice_bridge`` 归本模块，main_widget
侧必须以 ``tool_window._tool_reload_notice_bridge`` 模块属性方式读写，禁止
from-import（None → 实例的重新绑定会脱钩）。
"""

from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget
from qfluentwidgets import FluentIcon, IconWidget, TransparentToolButton

from app.utils.config import Settings
from app.utils.design_tokens import Colors, scale_font_size
from app.utils.utils import get_icon


class ToolWindowTitleBar(QWidget):
    """窗口标题栏（原 app/tool_popup.py 定义，随 ToolPopupDialog 下线迁移至此）"""

    popupRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._custom_buttons = []
        self._popup_mode_buttons = []
        self._is_compact = False
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(28)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 2, 0)
        layout.setSpacing(4)

        self._icon_widget = IconWidget(self)
        self._icon_widget.setFixedSize(20, 20)

        self._title_label = QLabel(self)
        self._title_label.setObjectName("titleLabel")

        layout.addWidget(self._icon_widget)
        layout.addWidget(self._title_label)
        layout.addStretch()

        self._action_container = QWidget(self)
        self._action_container.setObjectName("actionContainer")
        self._action_layout = QHBoxLayout(self._action_container)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(3)
        layout.addWidget(self._action_container)

        # 设置按钮已移除（移到主窗口内）

        self._min_btn = TransparentToolButton(get_icon("最小化"), self)
        self._min_btn.setFixedSize(28, 28)
        self._min_btn.setToolTip("最小化")

        self._popup_btn = TransparentToolButton(FluentIcon.CLOSE, self)
        self._popup_btn.setFixedSize(28, 28)
        self._popup_btn.setToolTip("关闭")
        self._popup_btn.clicked.connect(self._on_popup_clicked)

        layout.addWidget(self._min_btn)
        layout.addWidget(self._popup_btn)

        try:
            font_name = Settings.get_instance().llm_font_family.value
        except Exception:
            try:
                font_name = Settings.get_instance().canvas_font_selected.value
            except Exception:
                font_name = "Microsoft YaHei"

        # 使用主题颜色
        from app.utils.design_tokens import Colors

        Colors.refresh()
        Colors.refresh()
        title_color = Colors.TEXT_PRIMARY
        btn_hover = Colors.HOVER_BG
        border_color = Colors.BORDER

        self.setStyleSheet(f"""
            ToolWindowTitleBar {{
                background-color: {Colors.CONTENT_BG};
                border-bottom: 1px solid {border_color};
            }}
            #titleLabel {{
                color: {title_color};
                font-size: {scale_font_size(13)}px;
                font-weight: bold;
                font-family: "{font_name}";
                padding: 0 3px;
            }}
            #actionContainer {{
                background-color: transparent;
            }}
            ToolButton {{
                background-color: transparent;
                border: none;
                border-radius: 3px;
                padding: 1px;
            }}
            ToolButton:hover {{
                background-color: {btn_hover};
            }}
            ToolButton:pressed {{
                background-color: {btn_hover};
            }}
        """)

    def set_icon(self, icon):
        self._icon_widget.setIcon(icon)

    def set_title(self, title):
        self._title_label.setText(title)

    def set_title_color(self, color: str):
        """设置标题文字颜色（覆盖默认的 TEXT_PRIMARY）

        传入空字符串 '' 可清除行内颜色样式，恢复默认主题色。
        """
        if color:
            self._title_label.setStyleSheet(f"color: {color};")
        else:
            self._title_label.setStyleSheet("")

    def add_button(self, widget, stretch=0):
        self._action_layout.insertWidget(self._action_layout.count() - 2, widget, stretch=stretch)
        self._custom_buttons.append(widget)

    def insert_button(self, index, widget, stretch=0):
        self._action_layout.insertWidget(index, widget, stretch=stretch)
        self._custom_buttons.append(widget)

    def _on_popup_clicked(self):
        self.popupRequested.emit()

    def refresh_style(self):
        """主题/字体变更时刷新标题栏样式"""
        Colors.refresh()
        # 重新读取字体
        try:
            font_name = Settings.get_instance().llm_font_family.value
        except Exception:
            try:
                font_name = Settings.get_instance().canvas_font_selected.value
            except Exception:
                font_name = "Microsoft YaHei"

        title_color = Colors.TEXT_PRIMARY
        btn_hover = Colors.HOVER_BG
        border_color = Colors.BORDER

        # 整体标题栏样式
        self.setStyleSheet(f"""
            ToolWindowTitleBar {{
                background-color: {Colors.CONTENT_BG};
                border-bottom: 1px solid {border_color};
            }}
            #titleLabel {{
                color: {title_color};
                font-size: {scale_font_size(13)}px;
                font-weight: bold;
                font-family: "{font_name}";
                padding: 0 3px;
            }}
            #actionContainer {{
                background-color: transparent;
            }}
            ToolButton {{
                background-color: transparent;
                border: none;
                border-radius: 3px;
                padding: 1px;
            }}
            ToolButton:hover {{
                background-color: {btn_hover};
            }}
            ToolButton:pressed {{
                background-color: {btn_hover};
            }}
        """)


class ToolWindow(QWidget):
    """工具窗口基类（原 app/tool_popup.py 定义，随 ToolPopupDialog 下线迁移至此）"""

    name: str = "Unnamed"
    icon = None

    def __init__(self, page):
        super().__init__()
        self.homepage = page
        self._title_bar = None
        self._content_widget = None

        self._init_unified_font()
        self._init_title_bar()
        self.setObjectName("OpenAIChatToolWindow")

    def _init_title_bar(self):
        if self._title_bar:
            return

        self._title_bar = ToolWindowTitleBar(self)
        self._title_bar.set_icon(self.icon)
        self._title_bar.set_title(self.name)
        self._title_bar.hide()
        self._setup_title_bar()

    def _setup_title_bar(self):
        pass

    def get_title_bar(self):
        return self._title_bar

    def _init_unified_font(self):
        try:
            font_name = Settings.get_instance().llm_font_family.value
        except Exception:
            try:
                font_name = Settings.get_instance().canvas_font_selected.value
            except Exception:
                font_name = "Microsoft YaHei"

        font = self.font()
        font.setFamily(font_name)
        self.setFont(font)

        # 只设置字体，不设置背景（背景由子类的 setup_ui 处理）
        self.setStyleSheet(f"""
            ToolWindow {{
                font-family: "{font_name}";
            }}
            QLabel, QPushButton, QLineEdit, QComboBox, QTreeWidget, QTableWidget {{
                font-family: "{font_name}";
            }}
        """)


class _ToolReloadNoticeBridge(QObject):
    """工具热重载风险通知桥：watcher 后台线程 emit → 主线程槽执行

    reloaded 由 watcher 后台线程 emit；桥在主线程创建，reloaded→notified
    跨线程自动 QueuedConnection，确保外部槽在主线程执行。
    """

    reloaded = pyqtSignal()
    notified = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.reloaded.connect(self.notified)


_tool_reload_notice_bridge: Optional[_ToolReloadNoticeBridge] = None
