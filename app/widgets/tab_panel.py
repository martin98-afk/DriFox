# -*- coding: utf-8 -*-
"""
TabPanel — Tab 管理器左侧面板

每个 Tab 项显示：Agent 图标 + 会话标题 + 关闭按钮。
支持拖拽排序、右键菜单、滚轮滚动。
"""

from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QMouseEvent, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CaptionLabel, FluentIcon as FIF, PushButton, TransparentPushButton, TransparentToolButton, isDarkTheme

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css
from app.widgets.elided_label import _ElidedLabel


class TabItem(QFrame):
    """单个 Tab 项的 UI 组件"""

    closeRequested = pyqtSignal()

    def __init__(self, title: str, icon=None, parent=None):
        super().__init__(parent)
        self._title = title
        self._icon_pixmap = icon
        self._selected = False
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(6)

        # 图标
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(20, 20)
        if self._icon_pixmap:
            from PyQt5.QtGui import QPixmap

            if isinstance(self._icon_pixmap, QPixmap):
                self._icon_label.setPixmap(
                    self._icon_pixmap.scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            else:
                # QIcon 等类型：转为 QPixmap
                try:
                    pixmap = self._icon_pixmap.pixmap(20, 20)
                    if pixmap:
                        self._icon_label.setPixmap(pixmap)
                except Exception:
                    pass
        layout.addWidget(self._icon_label)

        # ── 团队角色胶囊（默认隐藏）──
        self._capsule_label = QLabel(self)
        self._capsule_label.setVisible(False)
        self._capsule_label.setFixedHeight(20)
        self._capsule_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        layout.addWidget(self._capsule_label)

        # 标题（使用 _ElidedLabel 自动省略，保证关闭按钮始终可见）
        self._title_label = _ElidedLabel(self._title, self)
        self._title_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; background: transparent;")
        layout.addWidget(self._title_label, 1)

        # 关闭按钮（与主标题栏一致的 FluentIcon.CLOSE）
        self._close_btn = TransparentToolButton(self)
        self._close_btn.setIcon(FIF.CLOSE)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setVisible(False)
        self._close_btn.clicked.connect(self.closeRequested.emit)
        layout.addWidget(self._close_btn)

    def set_selected(self, selected: bool):
        self._selected = selected
        self.update()

    def set_title(self, title: str):
        self._title = title
        self._title_label.setText(title)

    def set_icon(self, icon):
        self._icon_pixmap = icon
        if icon:
            from PyQt5.QtGui import QPixmap

            if isinstance(icon, QPixmap):
                self._icon_label.setPixmap(icon.scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                try:
                    pixmap = icon.pixmap(20, 20)
                    if pixmap:
                        self._icon_label.setPixmap(pixmap)
                except Exception:
                    pass

    def set_capsule(self, text: str, color: str = ""):
        """显示团队角色胶囊"""
        if not color:
            # 从 agent 名 hash 生成稳定色
            h = abs(hash(text)) % 360
            color = f"hsl({h}, 65%, 50%)"
        self._capsule_label.setText(text)
        self._capsule_label.setStyleSheet(f"""
            QLabel {{
                background: {color};
                color: white;
                border-radius: 4px;
                padding: 1px 6px;
                font-size: 11px;
                font-weight: bold;
            }}
        """)
        self._capsule_label.setVisible(True)

    def clear_capsule(self):
        """隐藏团队角色胶囊"""
        self._capsule_label.setVisible(False)
        self._capsule_label.setText("")

    def enterEvent(self, event):
        self._close_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._selected:
            self._close_btn.setVisible(False)
        super().leaveEvent(event)

    def paintEvent(self, event):
        from PyQt5.QtGui import QColor

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._selected:
            # 解析 rgba() 字符串为 QColor
            bg_str = Colors.SELECTED_BG
            try:
                if bg_str.startswith("rgba("):
                    parts = bg_str.strip("rgba() ").split(",")
                    r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                    a = int(float(parts[3]) * 255) if float(parts[3]) <= 1 else int(parts[3])
                    painter.fillRect(self.rect(), QColor(r, g, b, a))
                else:
                    painter.fillRect(self.rect(), QColor(bg_str))
            except Exception:
                painter.fillRect(self.rect(), QColor(102, 198, 255, 90))

            # 左侧选中指示条
            inf_str = Colors.INFO
            try:
                if inf_str.startswith("rgba("):
                    parts = inf_str.strip("rgba() ").split(",")
                    r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                    a = int(float(parts[3]) * 255) if len(parts) > 3 else 255
                    painter.fillRect(0, 4, 3, self.height() - 8, QColor(r, g, b, a))
                else:
                    painter.fillRect(0, 4, 3, self.height() - 8, QColor(inf_str))
            except Exception:
                painter.fillRect(0, 4, 3, self.height() - 8, QColor(255, 255, 255, 200))

        super().paintEvent(event)


class TabPanel(QWidget):
    """左侧 Tab 列表面板"""

    tabSelected = pyqtSignal(int)  # 选中 Tab 索引
    tabCloseRequested = pyqtSignal(int)  # 关闭 Tab 索引
    newTabRequested = pyqtSignal()  # 新建 Tab
    tabsReordered = pyqtSignal(list)  # 拖拽排序后新顺序（索引列表）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[TabItem] = []
        self._active_index: int = -1
        self._setup_ui()

    _SEPARATOR_STYLE = f"""
        QFrame {{
            background: {Colors.BORDER};
            max-height: 1px;
        }}
    """

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 顶部：新建按钮 ──
        top_bar = QWidget(self)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(6, 6, 6, 4)
        self._new_btn = TransparentPushButton(FIF.ADD, "新建标签页", top_bar)
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._new_btn.clicked.connect(self.newTabRequested.emit)
        top_layout.addWidget(self._new_btn)
        layout.addWidget(top_bar)

        # ── 中间：Tab 列表 ──
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setFrameShape(QFrame.NoFrame)

        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(6, 0, 6, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch()
        self._scroll_area.setWidget(self._list_widget)
        layout.addWidget(self._scroll_area, 1)

        # ── 分隔线 ──
        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet(self._SEPARATOR_STYLE)
        layout.addWidget(separator)

        # ── 底部：设置按钮 ──
        bottom_bar = QWidget(self)
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(6, 4, 6, 6)

        self._settings_btn = TransparentPushButton(FIF.SETTING, "设置", bottom_bar)
        self._settings_btn.setCursor(Qt.PointingHandCursor)
        self._settings_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._settings_btn.clicked.connect(self._on_settings_clicked)
        bottom_layout.addWidget(self._settings_btn)

        layout.addWidget(bottom_bar)

    def _on_settings_clicked(self):
        """打开设置卡片"""
        from PyQt5.QtWidgets import QWidget

        # 沿父链向上找 OpenAIChatToolWindow
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, "_toggle_settings_card"):
                parent._toggle_settings_card()
                return
            parent = parent.parent()
        # 兜底：通过 TabManagerWindow 切换回独立模式
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm:
            current = tm.get_current_window()
            if current and hasattr(current, "_toggle_settings_card"):
                current._toggle_settings_card()

    def add_tab(self, title: str, icon=None) -> int:
        """添加 Tab 项，返回其索引"""
        idx = len(self._items)
        item = TabItem(title, icon, self._list_widget)

        # 连接信号
        item.closeRequested.connect(lambda i=idx: self.tabCloseRequested.emit(i))

        # 连接点击事件
        def on_click(ev, i=idx):
            if ev.button() == Qt.LeftButton:
                self.set_active_index(i)

        item.mousePressEvent = on_click

        # 在 stretch 之前插入
        self._list_layout.insertWidget(idx, item)
        self._items.append(item)

        # 如果这是第一个 Tab，自动选中
        if len(self._items) == 1:
            self.set_active_index(0)

        return idx

    def remove_tab(self, index: int):
        """移除指定索引的 Tab"""
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self._list_layout.removeWidget(item)
            item.deleteLater()

            # 更新选中态
            if self._active_index == index:
                # 切换到相邻 Tab
                new_idx = min(index, len(self._items) - 1) if self._items else -1
                self.set_active_index(new_idx)
            elif self._active_index > index:
                self._active_index -= 1

    def set_active_index(self, index: int):
        """设置选中 Tab"""
        # 取消旧的选中态
        if 0 <= self._active_index < len(self._items):
            self._items[self._active_index].set_selected(False)
            self._items[self._active_index]._close_btn.setVisible(False)

        self._active_index = index

        # 设置新的选中态
        if 0 <= index < len(self._items):
            self._items[index].set_selected(True)
            self.tabSelected.emit(index)

    def update_tab_title(self, index: int, title: str):
        """更新 Tab 标题"""
        if 0 <= index < len(self._items):
            self._items[index].set_title(title)

    def update_tab_icon(self, index: int, icon):
        """更新 Tab 图标"""
        if 0 <= index < len(self._items):
            self._items[index].set_icon(icon)

    def update_tab_capsule(self, index: int, text: str):
        """显示团队角色胶囊"""
        if 0 <= index < len(self._items):
            self._items[index].set_capsule(text)

    def clear_tab_capsule(self, index: int):
        """隐藏团队角色胶囊"""
        if 0 <= index < len(self._items):
            self._items[index].clear_capsule()

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def active_index(self) -> int:
        return self._active_index

    def contextMenuEvent(self, event):
        """显示右键菜单"""
        if self._active_index < 0:
            return

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {Colors.CARD_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background: {Colors.HOVER_BG};
            }}
        """)
        close_action = menu.addAction("关闭标签页")
        menu.addSeparator()
        duplicate_action = menu.addAction("复制窗口")
        branch_action = menu.addAction("分支窗口")
        menu.addSeparator()
        rename_action = menu.addAction("重命名会话")

        action = menu.exec_(event.globalPos())
        if action == close_action:
            self.tabCloseRequested.emit(self._active_index)
