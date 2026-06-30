# -*- coding: utf-8 -*-
"""MarketplaceCard 浮动卡片 — 完整插件市场浏览界面"""
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
)

from .data import get_marketplace
from .installer import get_installer


class MarketplaceCard(QWidget):
    """插件市场浮动卡片

    显示可安装的插件列表，包含：
    - 刷新按钮
    - 插件列表（每个含安装/卸载按钮）
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()
        self.refresh()

    def _setup_ui(self):
        self.setMinimumWidth(480)
        self.setMinimumHeight(360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # 标题栏
        title = StrongBodyLabel("\U0001F4E6 插件市场", self)
        layout.addWidget(title)

        # 刷新按钮
        self._refresh_btn = PushButton(FluentIcon.SYNC, "刷新", self)
        self._refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(self._refresh_btn)

        # 滚动区域
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._content = QWidget(self._scroll)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(6)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        layout.addWidget(self._scroll, 1)

    def refresh(self):
        """刷新市场数据"""
        marketplace = get_marketplace()
        installer = get_installer()
        plugins = marketplace.list_plugins()
        # 清空旧内容
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for p in plugins:
            name = p.get("name", "")
            label_text = (
                f"\U0001F4E6 {name} v{p.get('version', '')} - "
                f"{p.get('description', '')[:60]}"
            )
            label = StrongBodyLabel(label_text, self._content)
            self._content_layout.addWidget(label)

            btn_text = "已安装" if installer.is_installed(name) else "安装"
            btn = PushButton(btn_text, self._content)
            btn.setEnabled(not installer.is_installed(name))
            btn.clicked.connect(lambda checked, pm=dict(p): self._install(pm, btn))
            self._content_layout.addWidget(btn)

    def _install(self, plugin_meta: dict, btn: PushButton):
        """安装插件"""
        btn.setEnabled(False)
        btn.setText("安装中...")
        installer = get_installer()
        success = installer.install(plugin_meta)
        if success:
            btn.setText("已安装")
        else:
            btn.setEnabled(True)
            btn.setText("安装失败，重试")
