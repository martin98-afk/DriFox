# -*- coding: utf-8 -*-
"""
LSP 状态卡片 — 在系统设置中展示已注册的 LSP 语言服务器及其运行状态

参考 MCPListSettingCard 模式，但更简单：只读展示，无需编辑/启停功能。
"""
from __future__ import annotations

from typing import Dict

from loguru import logger
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QWidget,
)
from qfluentwidgets import (
    CardWidget,
    Dialog,
    ExpandSettingCard,
    InfoBar,
    InfoBarPosition,
    PushButton,
    StrongBodyLabel,
    SwitchButton,
)

from app.utils.config import Settings
from app.utils.design_tokens import Colors, SwitchStyles, apply_font_size_to_widget, scale_font_size
from app.utils.utils import get_font_family_css, get_unified_font
from app.widgets.elided_label import _ElidedLabel

# ── LSP 单行 ──────────────────────────────────────────────────────


class LspServerRow(CardWidget):
    """LSP 服务器单行展示：状态点 + 名称 + 扩展名列表 + [安装按钮]"""

    installRequested = pyqtSignal(str, str)  # (server_name, install_hint)

    def __init__(self, name: str, extensions: list, is_running: bool,
                 install_hint: str = "", parent=None):
        super().__init__(parent)
        self._name = name
        self._extensions = extensions
        self._install_hint = install_hint
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
            # 运行中隐藏安装按钮
            if hasattr(self, '_install_btn'):
                self._install_btn.setVisible(False)
        else:
            self._status_dot.setText("●")
            self._status_dot.setStyleSheet(
                f"color: #6b7280; font-size: {scale_font_size(16)}px; "
                f"background: transparent; padding: 0;"
            )
            self._status_dot.setToolTip("未启动")
            # 未运行时，若未安装则显示安装按钮
            if hasattr(self, '_install_btn'):
                self._install_btn.setVisible(bool(self._install_hint))

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

        # 安装按钮（仅当有 installHint 时创建，不占用空间直到可见）
        self._install_btn = PushButton("安装", self)
        self._install_btn.setFixedWidth(56)
        self._install_btn.setFixedHeight(24)
        self._install_btn.setStyleSheet(
            f"font-size: {scale_font_size(11)}px; "
            f"padding: 2px 8px;"
        )
        self._install_btn.setToolTip(
            f"执行: {self._install_hint}" if self._install_hint else "未提供安装命令"
        )
        self._install_btn.clicked.connect(
            lambda: self.installRequested.emit(self._name, self._install_hint)
        )
        self._install_btn.setVisible(False)
        layout.addWidget(self._install_btn)


# ── LSP 列表卡片 ────────────────────────────────────────────────


class LspListSettingCard(ExpandSettingCard):
    """LSP 语言服务器状态卡片 — 展示已注册的 LSP 服务器及其运行状态"""

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
        self.viewLayout.setSpacing(0)
        self.viewLayout.setAlignment(Qt.AlignTop)
        self.viewLayout.setContentsMargins(8, 0, 8, 0)
        self.view.setStyleSheet("background-color: transparent;")

        # 自动诊断开关
        self._auto_diag_row = QWidget(self)
        self._auto_diag_row.setStyleSheet("background-color: transparent;")
        row_layout = QHBoxLayout(self._auto_diag_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(2)

        diag_desc = QLabel("自动诊断")
        diag_desc.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        diag_desc.setToolTip(
            "开启后，write / edit / multi_edit 编辑文件时，"
            "若文件后缀命中已注册 LSP 服务器，自动运行诊断并随编辑结果返回"
        )
        row_layout.addWidget(diag_desc)

        self._auto_diag_switch = SwitchButton(self._auto_diag_row)
        SwitchStyles.configure(self._auto_diag_switch)
        self._auto_diag_switch.setChecked(self.cfg.lsp_auto_diagnose.value)
        self._auto_diag_switch.checkedChanged.connect(self._on_auto_diag_toggled)
        row_layout.addWidget(self._auto_diag_switch)

        self.addWidget(self._auto_diag_row)

    def _on_auto_diag_toggled(self, checked: bool):
        """自动诊断开关切换"""
        self.cfg.lsp_auto_diagnose.value = checked
        self.cfg.save()

    def _rebuild(self):
        """重建列表（清空 + 重新创建行）"""
        was_expanded = self.isExpand

        self._rows.clear()

        # 清空旧 widget
        while self.viewLayout.count():
            item = self.viewLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 更新开关状态（与配置同步）
        self._auto_diag_switch.blockSignals(True)
        self._auto_diag_switch.setChecked(self.cfg.lsp_auto_diagnose.value)
        self._auto_diag_switch.blockSignals(False)

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
                # 检查二进制是否可用
                is_installed = client.is_command_available()
                install_hint = (
                    "" if is_installed
                    else client.config.install_hint
                )
                row = LspServerRow(
                    name, exts,
                    client.is_running,
                    install_hint=install_hint,
                    parent=self.view,
                )
                row.installRequested.connect(self._on_install_requested)
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
        """定时刷新——配置变化时自动重建列表，否则只更新状态灯"""
        mgr = self._get_lsp_manager()
        if not mgr:
            return

        # 检测 LSP 配置是否发生变化（插件热重载增删了服务器）
        current_names = set(mgr._clients.keys())
        card_names = set(self._rows.keys())
        if current_names != card_names:
            self._rebuild()
            return

        # 只更新现有的运行/安装状态
        for name, row in self._rows.items():
            client = mgr._clients.get(name)
            if client:
                row.set_running(client.is_running)
                if not client.is_running and not client.is_command_available():
                    row._install_hint = client.config.install_hint

    def _on_install_requested(self, server_name: str, install_hint: str):
        """处理安装按钮点击 — 在终端中执行安装命令"""
        if not install_hint:
            InfoBar.warning(
                title="安装命令不可用",
                content=f"LSP 服务器「{server_name}」未提供安装命令。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            return

        # 将 installHint 中的 pip 替换为当前环境的包管理器
        # 优先 uv（项目标准），其次 python -m pip，最后原样
        import shutil
        import sys
        _cmd = install_hint
        if _cmd.startswith("pip "):
            pkg = _cmd[4:]  # "install pyright"
            if shutil.which("uv"):
                _cmd = f"uv pip {pkg}"
            elif shutil.which("pip"):
                _cmd = install_hint  # 原样
            else:
                _python = sys.executable
                _cmd = f'"{_python}" -m pip {pkg}'

        # 确认对话框（使用 qfluentwidgets.Dialog，自动应用 Fluent 主题色，
        # 避免 PyQt5 原生 QMessageBox 在深色主题下显示为系统默认全黑窗口）
        w = Dialog(
            f"安装 LSP 服务器 — {server_name}",
            f"即将在终端中执行以下安装命令：\n\n"
            f"    {_cmd}\n\n"
            f"安装完成后请点击「刷新」按钮重新连接。\n\n"
            f"是否继续？",
            self.window(),
        )
        w.yesButton.setText("确定")
        w.cancelButton.setText("取消")
        w.setContentCopyable(True)  # 安装命令可选中复制

        if not w.exec_():
            return

        import os
        import subprocess

        try:
            if sys.platform == "win32":
                # start "" cmd /k  — 空标题，避免引号嵌套解析错误
                subprocess.Popen(
                    ["cmd", "/c", "start", "", "cmd", "/k", _cmd],
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            elif sys.platform == "darwin":
                # macOS: 用 osascript 打开 Terminal
                script = (
                    f'tell application "Terminal" to do script "{_cmd}"'
                )
                subprocess.Popen(["osascript", "-e", script])
            else:
                # Linux: 尝试常见终端模拟器
                terminals = ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"]
                launched = False
                for term in terminals:
                    if os.system(f"which {term} > /dev/null 2>&1") == 0:
                        if term == "gnome-terminal":
                            subprocess.Popen([term, "--", "bash", "-c", f"{_cmd}; exec bash"])
                        else:
                            subprocess.Popen([term, "-e", f"{_cmd}; exec bash"])
                        launched = True
                        break
                if not launched:
                    InfoBar.error(
                        title="无法找到终端",
                        content=f"请手动在终端中执行: {_cmd}",
                        orient=Qt.Horizontal,
                        isClosable=True,
                        position=InfoBarPosition.TOP,
                        duration=8000,
                        parent=self,
                    )
                    return

            InfoBar.success(
                title=f"正在安装 {server_name}",
                content="已在终端中打开安装进程。完成后请点击「刷新」按钮。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
        except Exception as e:
            logger.error(f"[LspCard] 安装启动失败: {e}")
            InfoBar.error(
                title="安装启动失败",
                content=str(e),
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=8000,
                parent=self,
            )

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
