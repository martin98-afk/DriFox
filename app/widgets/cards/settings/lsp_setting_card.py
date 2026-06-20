# -*- coding: utf-8 -*-
"""
LSP 状态卡片 — 在系统设置中展示已注册的 LSP 语言服务器及其运行状态

参考 MCPListSettingCard 模式，但更简单：只读展示，无需编辑/启停功能。
"""
from __future__ import annotations

from typing import Dict

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
)
from loguru import logger
from qfluentwidgets import (
    CardWidget,
    ExpandSettingCard,
    StrongBodyLabel,
    PushButton,
    FluentIcon,
)

from app.utils.config import Settings
from app.utils.design_tokens import Colors, scale_font_size, font_size_css
from app.utils.design_tokens import apply_font_size_to_widget
from app.utils.utils import get_font_family_css, get_unified_font
from app.widgets.elided_label import _ElidedLabel


# ── LSP 单行 ──────────────────────────────────────────────────────


class LspServerRow(CardWidget):
    """LSP 服务器单行展示：状态点 + 名称 + 扩展名列表"""

    def __init__(self, name: str, extensions: list, is_running: bool, parent=None):
        super().__init__(parent)
        self._name = name
        self._extensions = extensions
        self._setup_ui()
        self.set_running(is_running)

    def set_running(self, running: bool):
        """更新运行状态指示灯"""
        if running:
            self._status_dot.setText("●")
            self._status_dot.setStyleSheet(
                f"color: #22c55e; font-size: {scale_font_size(16)}px; "
                f"background: transparent; padding: 0;"
            )
            self._status_dot.setToolTip("运行中")
        else:
            self._status_dot.setText("●")
            self._status_dot.setStyleSheet(
                f"color: #6b7280; font-size: {scale_font_size(16)}px; "
                f"background: transparent; padding: 0;"
            )
            self._status_dot.setToolTip("未启动")

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)

        # 状态指示灯
        self._status_dot = QLabel("●")
        self._status_dot.setFixedWidth(16)
        self._status_dot.setAlignment(Qt.AlignCenter)
        self._status_dot.setToolTip("未启动")
        self._status_dot.setStyleSheet(
            f"color: #6b7280; font-size: {scale_font_size(16)}px; "
            f"background: transparent; padding: 0;"
        )
        layout.addWidget(self._status_dot)

        # 名称
        name_label = StrongBodyLabel(self._name)
        name_label.setFixedWidth(120)
        name_label.setFont(get_unified_font(11))
        layout.addWidget(name_label)

        # 扩展名列表
        exts = ", ".join(self._extensions[:8])
        if len(self._extensions) > 8:
            exts += f" +{len(self._extensions) - 8}"

        ext_label = _ElidedLabel(exts)
        ext_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        ext_label.setMinimumWidth(40)
        ext_label.setToolTip(", ".join(self._extensions))
        layout.addWidget(ext_label, 1)


# ── LSP 列表卡片 ────────────────────────────────────────────────


class LspListSettingCard(ExpandSettingCard):
    """LSP 语言服务器状态卡片 — 只读展示已注册的 LSP 服务器"""

    _hotUpdateRequested = pyqtSignal()

    def __init__(self, icon, title: str, content: str = None, parent=None):
        self.cfg = Settings.get_instance()
        super().__init__(icon, title, content, parent)

        self._rows: Dict[str, LspServerRow] = {}

        # 状态刷新定时器（3秒）
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(3000)
        self._refresh_timer.timeout.connect(self._refresh_status)

        self._setup_ui()
        self._rebuild()
        # 首次加载后延迟刷新一次状态
        QTimer.singleShot(500, self._refresh_status)
        self._refresh_timer.start()

    def _get_lsp_manager(self):
        """获取 LspManager 实例"""
        try:
            from app.core.lsp.lsp_manager import LspManager
            return LspManager.get_instance()
        except Exception:
            return None

    def _setup_ui(self):
        self.viewLayout.setSpacing(2)
        self.viewLayout.setAlignment(Qt.AlignTop)
        self.viewLayout.setContentsMargins(8, 0, 8, 0)
        self.view.setStyleSheet("background-color: transparent;")

        # 刷新按钮
        self.refreshButton = PushButton("刷新", self, FluentIcon.SYNC)
        self.refreshButton.clicked.connect(self._on_refresh)
        self.addWidget(self.refreshButton)

    def _on_refresh(self):
        """手动刷新：重新从 PluginManager 加载配置并重建列表"""
        try:
            # 重新加载 LSP 配置
            from app.core.plugin_manager import PluginManager
            pm = PluginManager.get_instance()
            if pm.is_initialized():
                mgr = self._get_lsp_manager()
                if mgr:
                    cfgs = pm.get_lsp_configs()
                    mgr.initialize(mgr._workspace_root, cfgs)
        except Exception as e:
            logger.error(f"[LspCard] 刷新配置失败: {e}")

        self._rebuild()
        QTimer.singleShot(300, self._refresh_status)

    def _rebuild(self):
        """重建列表（清空 + 重新创建行）"""
        was_expanded = self.isExpand

        self._rows.clear()

        # 清空旧 widget
        while self.viewLayout.count():
            item = self.viewLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        mgr = self._get_lsp_manager()
        if not mgr or not mgr._clients:
            empty_label = QLabel("暂无 LSP 服务器", self.view)
            empty_label.setStyleSheet(
                f"color: #888; padding: 16px; "
                f"{get_font_family_css()} font-size: {scale_font_size(12)}px;"
            )
            empty_label.setAlignment(Qt.AlignCenter)
            self.viewLayout.addWidget(empty_label)
        else:
            for name, client in mgr._clients.items():
                exts = list(client.config.extension_to_language.keys())
                row = LspServerRow(name, exts, client.is_running, self.view)
                self._rows[name] = row
                self.viewLayout.addWidget(row)

        from PyQt5.QtCore import QCoreApplication
        QCoreApplication.processEvents()
        self.viewLayout.activate()
        self.view.updateGeometry()
        self._adjustViewSize()

        if was_expanded:
            h = self.viewLayout.sizeHint().height()
            if h > 0:
                self.setFixedHeight(self.card.height() + h)

        apply_font_size_to_widget(self, 14)

    def _refresh_status(self):
        """定时刷新运行状态指示灯（轻量更新，不重建列表）"""
        mgr = self._get_lsp_manager()
        if not mgr:
            return

        for name, row in self._rows.items():
            client = mgr._clients.get(name)
            if client:
                row.set_running(client.is_running)

    def showEvent(self, event):
        """卡片显示时恢复状态轮询"""
        super().showEvent(event)
        self._refresh_timer.start()
        self._refresh_status()

    def hideEvent(self, event):
        """卡片隐藏时停止状态轮询"""
        super().hideEvent(event)
        self._refresh_timer.stop()

    def closeEvent(self, event):
        self._refresh_timer.stop()
        super().closeEvent(event)
