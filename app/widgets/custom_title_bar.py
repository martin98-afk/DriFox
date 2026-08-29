"""自定义无边框窗口标题栏

布局：[侧栏开关] ... [中央自定义 tab 区] ... [最小化][最大化][关闭]
- Windows：右侧三按钮用 TitleBarBase 内置自绘按钮（close hover 红）
- macOS：隐藏三按钮（系统交通灯渲染于左上），左区预留 70px
- 顶部 tab 为可注册扩展点：add_tab/remove_tab/set_active_tab + tab_clicked 信号
- 主题适配：颜色全部取自 Colors 主题 token，Colors.refresh() 后由 refresh_style() 重建 qss
"""

import sys
from typing import Callable, Dict, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from qframelesswindow.titlebar import TitleBarBase

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon


class CustomTabButton(QPushButton):
    """顶栏胶囊 tab 按钮（激活态高亮，样式随主题 token 刷新）"""

    def __init__(self, tab_id: str, text: str, parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.tab_id = tab_id
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(26)

    def refresh_style(self):
        """按当前主题 token 重建样式（Colors.refresh() 由调用方负责）"""
        if self.isChecked():
            self.setStyleSheet(
                "QPushButton {"
                f" background: {Colors.TAB_ACTIVE_BG}; color: {Colors.TEXT_PRIMARY};"
                " border: none; border-radius: 13px; padding: 0 16px;"
                f" {font_size_css(13)}; font-weight: bold; }}"
            )
        else:
            self.setStyleSheet(
                "QPushButton {"
                f" background: transparent; color: {Colors.TEXT_SECONDARY};"
                " border: none; border-radius: 13px; padding: 0 16px;"
                f" {font_size_css(13)}; }}"
                "QPushButton:hover {"
                f" background: {Colors.TAB_HOVER_BG}; color: {Colors.TEXT_PRIMARY}; }}"
            )


class CustomTitleBar(TitleBarBase):
    """无边框窗口自定义标题栏

    TitleBarBase 已内置：minBtn/maxBtn/closeBtn、拖拽移动（canDrag 排除按钮区）、
    双击最大化/还原。本类只负责三区布局与 tab 扩展 API。
    """

    HEIGHT = 38
    MAC_TRAFFIC_LIGHT_PAD = 70  # macOS 系统交通灯左侧留白

    tab_clicked = pyqtSignal(str)
    sidebar_toggle_requested = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self._is_mac: bool = sys.platform == "darwin"
        self._tabs: Dict[str, CustomTabButton] = {}
        self._active_id: Optional[str] = None

        self.setFixedHeight(self.HEIGHT)

        # ── 左区：侧栏开关（透明无边框，样式随主题） + 品牌（自 TabPanel 移入） ──
        self._sidebar_btn = QPushButton(self)
        self._sidebar_btn.setIcon(get_icon("侧边栏"))
        self._sidebar_btn.setFixedSize(30, 26)
        self._sidebar_btn.setCursor(Qt.PointingHandCursor)
        self._sidebar_btn.setToolTip("收起/展开侧边栏")
        self._sidebar_btn.clicked.connect(self.sidebar_toggle_requested.emit)

        self._brand_title = QLabel("DriFox", self)
        self._brand_version = QLabel(Settings.current_version, self)

        # ── 中央区：tab 容器 ──
        self._tab_container = QWidget(self)
        self._tab_layout = QHBoxLayout(self._tab_container)
        self._tab_layout.setContentsMargins(0, 0, 0, 0)
        self._tab_layout.setSpacing(6)

        # ── 三区布局（mac 隐藏系统按钮 + 左侧留白给交通灯） ──
        left_pad = self.MAC_TRAFFIC_LIGHT_PAD if self._is_mac else 8
        layout = QHBoxLayout(self)
        layout.setContentsMargins(left_pad, 6, 0, 6)
        layout.setSpacing(4)
        layout.addWidget(self._sidebar_btn)
        layout.addSpacing(4)
        layout.addWidget(self._brand_title)
        layout.addWidget(self._brand_version)
        layout.addStretch(1)
        layout.addWidget(self._tab_container, 0, Qt.AlignCenter)
        layout.addStretch(1)
        if not self._is_mac:
            layout.addWidget(self.minBtn)
            layout.addWidget(self.maxBtn)
            layout.addWidget(self.closeBtn)
        else:
            self.minBtn.hide()
            self.maxBtn.hide()
            self.closeBtn.hide()

        self.refresh_style()

    # ── tab 扩展 API ──

    def add_tab(
        self,
        tab_id: str,
        text: str,
        icon: Optional[QIcon] = None,
        on_click: Optional[Callable] = None,
    ) -> None:
        """注册顶部 tab；首个注册的 tab 自动激活"""
        if tab_id in self._tabs:
            return
        btn = CustomTabButton(tab_id, text, self._tab_container)
        if icon is not None:
            btn.setIcon(icon)
        btn.clicked.connect(lambda _checked=False, tid=tab_id: self._on_tab_clicked(tid))
        if on_click is not None:
            btn.clicked.connect(lambda _checked=False: on_click())
        self._tab_layout.addWidget(btn)
        self._tabs[tab_id] = btn
        btn.refresh_style()
        if self._active_id is None:
            self.set_active_tab(tab_id)

    def remove_tab(self, tab_id: str) -> None:
        """移除 tab；若移除的是激活 tab 则自动激活剩余第一个"""
        btn = self._tabs.pop(tab_id, None)
        if btn is None:
            return
        self._tab_layout.removeWidget(btn)
        btn.deleteLater()
        if self._active_id == tab_id:
            self._active_id = None
            if self._tabs:
                self.set_active_tab(next(iter(self._tabs)))

    def set_active_tab(self, tab_id: str) -> None:
        """设置激活 tab（胶囊高亮）"""
        if tab_id not in self._tabs:
            return
        self._active_id = tab_id
        for tid, b in self._tabs.items():
            b.setChecked(tid == tab_id)
            b.refresh_style()

    def refresh_style(self) -> None:
        """主题切换后刷新样式（Colors.refresh() 由调用方先执行）"""
        for b in self._tabs.values():
            b.refresh_style()
        # 侧栏开关：透明背景无边框，仅 hover 显底
        self._sidebar_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 6px; padding: 3px; }"
            f"QPushButton:hover {{ background: {Colors.TAB_HOVER_BG}; }}"
        )
        # 品牌：标题 + 版本号（样式对齐 TabPanel 原品牌区）
        self._brand_title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {font_size_css(15)}; font-weight: bold; background: transparent;"
        )
        self._brand_version.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)}"
        )

    # ── 内部 ──

    def _on_tab_clicked(self, tab_id: str) -> None:
        self.set_active_tab(tab_id)
        self.tab_clicked.emit(tab_id)
