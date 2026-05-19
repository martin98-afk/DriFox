# -*- coding: utf-8 -*-

import psutil
from PyQt5.QtCore import pyqtSignal, QTimer, Qt
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QLabel
from qfluentwidgets import (
    IconWidget,
)
from qfluentwidgets import (
    isDarkTheme,
    FluentIcon as FIF,
    TransparentToolButton,
)

from app.utils.config import Settings
from app.utils.design_tokens import get_font_family_css
from app.utils.design_tokens import scale_font_size
from app.utils.utils import get_icon


class ToolWindowTitleBar(QWidget):
    popupRequested = pyqtSignal()
    lockRequested = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._custom_buttons = []
        self._popup_mode_buttons = []
        self._is_compact = False
        self._is_locked = False
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(32)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(6)

        self._icon_widget = IconWidget(self)
        self._icon_widget.setFixedSize(16, 16)

        self._title_label = QLabel(self)
        self._title_label.setObjectName("titleLabel")

        layout.addWidget(self._icon_widget)
        layout.addWidget(self._title_label)
        layout.addStretch()

        self._action_container = QWidget(self)
        self._action_container.setObjectName("actionContainer")
        self._action_layout = QHBoxLayout(self._action_container)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(4)
        layout.addWidget(self._action_container)

        # 内存显示标签
        self._memory_label = QLabel(self)
        self._memory_label.setObjectName("memoryLabel")
        self._memory_label.setFixedHeight(22)
        self._memory_label.setStyleSheet(f"color: #ffffff; {get_font_family_css()} font-size: {scale_font_size(12)}px; padding: 2px 6px; background-color: transparent; border: none; border-radius: 4px;")
        self._memory_label.hide()  # 默认隐藏，子类可以控制显示
        layout.insertWidget(layout.indexOf(self._action_container) - 1, self._memory_label)

        # 内存刷新定时器
        self._memory_timer = QTimer(self)
        self._memory_timer.setInterval(5000)  # 5秒刷新
        self._memory_timer.timeout.connect(self._update_memory_label)
        self._memory_refreshing = False

        # 设置按钮已移除（移到主窗口内）

        self._min_btn = TransparentToolButton(get_icon("最小化"), self)
        self._min_btn.setFixedSize(24, 24)
        self._min_btn.setToolTip("最小化")

        self._popup_btn = TransparentToolButton(FIF.CLOSE, self)
        self._popup_btn.setFixedSize(24, 24)
        self._popup_btn.setToolTip("关闭")
        self._popup_btn.clicked.connect(self._on_popup_clicked)

        # 锁定按钮 - 用于穿透模式
        self._lock_btn = TransparentToolButton(get_icon("解锁"), self)
        self._lock_btn.setFixedSize(24, 24)
        self._lock_btn.setToolTip("锁定窗口（鼠标穿透）")
        self._lock_btn.clicked.connect(self._on_lock_clicked)

        layout.addWidget(self._min_btn)
        layout.addWidget(self._popup_btn)
        layout.addWidget(self._lock_btn)

        try:
            font_name = Settings.get_instance().llm_font_family.value
        except Exception:
            try:
                font_name = Settings.get_instance().canvas_font_selected.value
            except Exception:
                font_name = "Microsoft YaHei"

        if isDarkTheme():
            bg = "#2d2d2d"
            title_color = "#e0e0e0"
            btn_hover = "rgba(255, 255, 255, 15)"
            border_color = "#3a3a3a"
        else:
            bg = "#f5f5f5"
            title_color = "#333333"
            btn_hover = "rgba(0, 0, 0, 10)"
            border_color = "#e0e0e0"

        self.setStyleSheet(f"""
            ToolWindowTitleBar {{
                background-color: {bg};
                border-bottom: 1px solid {border_color};
            }}
            #titleLabel {{
                color: {title_color};
                font-size: {scale_font_size(15)}px;
                font-weight: bold;
                font-family: "{font_name}";
                padding: 0 4px;
            }}
            /* #memoryLabel {{
                color: #ffffff;
                font-size: 12px;
                padding: 2px 6px;
                background-color: rgba(0, 0, 0, 20);
                border-radius: 4px;
            }} */
            #actionContainer {{
                background-color: transparent;
            }}
            ToolButton {{
                background-color: transparent;
                border: none;
                border-radius: 4px;
                padding: 2px;
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

    def add_button(self, widget, stretch=0):
        self._action_layout.insertWidget(
            self._action_layout.count() - 2, widget, stretch=stretch
        )
        self._custom_buttons.append(widget)

    def insert_button(self, index, widget, stretch=0):
        self._action_layout.insertWidget(index, widget, stretch=stretch)
        self._custom_buttons.append(widget)

    def remove_button(self, widget):
        self._action_layout.removeWidget(widget)
        if widget in self._custom_buttons:
            self._custom_buttons.remove(widget)
        widget.setParent(None)

    def _on_popup_clicked(self):
        self.popupRequested.emit()

    def _on_lock_clicked(self):
        self._is_locked = not self._is_locked
        self._update_lock_state()
        self.lockRequested.emit(self._is_locked)

    def _update_lock_state(self):
        """更新锁定按钮的图标和样式"""
        if self._is_locked:
            self._lock_btn.setIcon(get_icon("锁定"))
            self._lock_btn.setToolTip("取消锁定（恢复交互）")
            self._lock_btn.setStyleSheet("""
                QToolButton {
                    background-color: rgba(0, 120, 212, 180);
                    border-radius: 4px;
                }
                QToolButton:hover {
                    background-color: rgba(0, 120, 212, 220);
                }
            """)
        else:
            self._lock_btn.setIcon(get_icon("解锁"))
            self._lock_btn.setToolTip("锁定窗口（鼠标穿透）")
            self._lock_btn.setStyleSheet("")

    def set_locked(self, locked: bool):
        """外部设置锁定状态"""
        self._is_locked = locked
        self._update_lock_state()

    def is_locked(self) -> bool:
        return self._is_locked

    def show_memory_label(self):
        """显示内存标签并开始刷新"""
        self._memory_label.show()
        # 每次显示都重新启动定时器，确保新窗口独立刷新
        self._memory_timer.stop()
        self._memory_refreshing = True
        self._update_memory_label()
        self._memory_timer.start()

    def _update_memory_label(self):
        """更新内存显示"""
        try:
            process = psutil.Process()
            mem_info = process.memory_info()
            mem_mb = mem_info.rss / (1024 * 1024)
            self._memory_label.setText(f" {mem_mb:.0f} MB ")
        except Exception:
            self._memory_label.setText(" N/A ")


class ToolWindow(QWidget):
    name: str = "Unnamed"
    icon = None
    singleton = True

    def __init__(self, page, button):
        super().__init__()
        self.homepage = page
        self.button = button
        self._title_bar = None
        self._content_widget = None
        self._layout_mode = "vertical"

        self._init_unified_font()

        self._init_title_bar()

    def _init_title_bar(self):
        if self._title_bar:
            return

        self._title_bar = ToolWindowTitleBar(self)
        self._title_bar.set_icon(self.icon)
        self._title_bar.set_title(self.name)
        self._title_bar.popupRequested.connect(self._request_popup)
        self._title_bar.lockRequested.connect(self._on_window_lock_changed)
        self._title_bar.hide()
        self._setup_title_bar()

    def _setup_title_bar(self):
        pass

    def _toggle_layout(self):
        if self._layout_mode == "vertical":
            self._layout_mode = "horizontal"
        else:
            self._layout_mode = "vertical"
        self._on_layout_changed()

    def _on_layout_changed(self):
        pass

    def _request_popup(self):
        if hasattr(self.homepage, "_handle_tool_popup"):
            self.homepage._handle_tool_popup(self.name)

    def _on_window_lock_changed(self, locked: bool):
        """处理窗口锁定状态变化"""
        # ToolWindow 本身不处理穿透，
        # 穿透由容器（如弹窗）处理
        pass

    def _set_passthrough_mode(self, enabled: bool):
        """
        设置鼠标穿透模式 - 仅影响子控件层级
        不对窗口本身设置穿透，保持窗口可移动
        """
        def set_passthrough(widget, enable):
            # 只对子控件设置穿透，不对自身设置
            for child in widget.children():
                if isinstance(child, QWidget):
                    if child is self._title_bar:
                        continue
                    child.setAttribute(Qt.WA_TransparentForMouseEvents, enable)
                    set_passthrough(child, enable)

        set_passthrough(self, enabled)

    def register_action_button(self, widget):
        if self._title_bar:
            self._title_bar.add_button(widget)

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

        self.setStyleSheet(f"""
            ToolWindow, QWidget {{
                font-family: "{font_name}";
            }}
            QLabel, QPushButton, QLineEdit, QComboBox, QTreeWidget, QTableWidget {{
                font-family: "{font_name}";
            }}
        """)