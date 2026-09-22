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
    confirmRequested = pyqtSignal(str, bool)  # (gate_key, allow) —— 安全门禁确认

    def __init__(self, name: str, extensions: list, is_running: bool, install_hint: str = "", parent=None):
        super().__init__(parent)
        self._name = name
        self._extensions = extensions
        self._install_hint = install_hint
        self._gate_key = ""
        self._setup_ui()
        self.set_running(is_running)

    def set_gate_pending(self, gate_key: str) -> None:
        """标记该服务器处于「待安全确认」：显示放行按钮

        背景（EU-G22）：非内置源（用户级插件）的 LSP server 首启会被门禁判
        need_confirm；此前 LSP 侧无确认入口 → 用户自装插件永远启不了
        （`confirm_by_key` 仅 MCP 侧有调用）。此按钮补上该入口。
        """
        self._gate_key = gate_key or ""
        if hasattr(self, "_confirm_btn"):
            self._confirm_btn.setVisible(bool(self._gate_key))
            if self._gate_key:
                self._confirm_btn.setToolTip("该服务器来自用户级插件，首次启动需确认")

    def clear_gate_pending(self) -> None:
        self.set_gate_pending("")

    def set_running(self, running: bool):
        """更新运行状态指示灯"""
        if running:
            self._status_dot.setText("●")
            self._status_dot.setStyleSheet(
                f"color: #22c55e; font-size: {scale_font_size(16)}px; background: transparent; padding: 0;"
            )
            self._status_dot.setToolTip("运行中")
            # 运行中隐藏安装按钮
            if hasattr(self, "_install_btn"):
                self._install_btn.setVisible(False)
        else:
            self._status_dot.setText("●")
            self._status_dot.setStyleSheet(
                f"color: #6b7280; font-size: {scale_font_size(16)}px; background: transparent; padding: 0;"
            )
            self._status_dot.setToolTip("未启动")
            # 未运行时，若未安装则显示安装按钮
            if hasattr(self, "_install_btn"):
                self._install_btn.setVisible(bool(self._install_hint))

    def refresh_style(self):
        """主题变更时刷新扩展名后缀文字颜色"""
        Colors.refresh()
        self._ext_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )

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
            f"color: #6b7280; font-size: {scale_font_size(16)}px; background: transparent; padding: 0;"
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

        self._ext_label = _ElidedLabel(exts)
        self._ext_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        self._ext_label.setMinimumWidth(40)
        self._ext_label.setToolTip(", ".join(self._extensions))
        layout.addWidget(self._ext_label, 1)

        # 安装按钮（仅当有 installHint 时创建，不占用空间直到可见）
        self._install_btn = PushButton("安装", self)
        self._install_btn.setFixedWidth(56)
        self._install_btn.setFixedHeight(24)
        self._install_btn.setStyleSheet(f"font-size: {scale_font_size(11)}px; padding: 2px 8px;")
        self._install_btn.setToolTip(f"执行: {self._install_hint}" if self._install_hint else "未提供安装命令")
        self._install_btn.clicked.connect(lambda: self.installRequested.emit(self._name, self._install_hint))
        self._install_btn.setVisible(False)
        layout.addWidget(self._install_btn)

        # 安全确认按钮（仅当该服务器被门禁判 need_confirm 时显示）
        self._confirm_btn = PushButton("放行", self)
        self._confirm_btn.setFixedWidth(56)
        self._confirm_btn.setFixedHeight(24)
        self._confirm_btn.setStyleSheet(f"font-size: {scale_font_size(11)}px; padding: 2px 8px;")
        self._confirm_btn.setToolTip("该服务器来自用户级插件，首次启动需确认")
        self._confirm_btn.clicked.connect(lambda: self.confirmRequested.emit(self._gate_key, True))
        self._confirm_btn.setVisible(False)
        layout.addWidget(self._confirm_btn)


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
        # 将 refresh_style 指向私有方法（避免在 _setup_ui 前定义）
        self.refresh_style = self._refresh_lsp_style
        # 列表行延迟到首次展开时构建（见 _ensure_built）：_get_lsp_manager()
        # 会首次导入 app.core.lsp.lsp_manager，实测约 0.27s，折叠态下不必付
        self._built = False
        # ★ 性能（2026-09-13）：构造期既不预热 LSP 管理器、也不启动 3s 轮询。
        #   原来这里有两笔主线程开销，都会落在「设置卡刚显示」这一刻：
        #     ① QTimer.singleShot(500, _refresh_status) —— 卡片显示后 0.5s 才在
        #        主线程补首次 _get_lsp_manager()（首次 import lsp_manager 及依赖链
        #        ~0.27s），用户表现为"卡片刚出来又卡一下"；
        #     ② _refresh_timer.start() —— 折叠态、甚至设置弹窗从未打开时也在跑。
        #   两者都与本类既有设计意图（注释上一条：折叠态下不必付这份开销）相悖，
        #   改为统一在展开时开始，收起/隐藏即停止（见 setExpand / hideEvent）。

    def _ensure_built(self):
        """首次需要时构建列表行（幂等）"""
        if self._built:
            return
        self._built = True
        self._rebuild()

    def setExpand(self, isExpand: bool):
        """展开前补齐列表行，保证展开动画算到的是完整高度

        展开同时启动状态轮询并立即取一次真实状态（此时 lsp_manager 已被
        _rebuild 导入，属缓存命中，不再付导入成本）；收起即停轮询。
        """
        if isExpand:
            self._ensure_built()
            self._refresh_status()
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
        else:
            self._refresh_timer.stop()
        super().setExpand(isExpand)

    def hideEvent(self, event):
        """卡片不可见（含设置弹窗收起）时停止轮询，避免后台空转"""
        self._refresh_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        """重新可见时，仅当仍处于展开态才恢复轮询"""
        super().showEvent(event)
        if self.isExpand and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _get_lsp_manager(self):
        """获取 LspManager 实例"""
        try:
            from app.core.lsp.lsp_manager import get_lsp_manager

            return get_lsp_manager()
        except Exception:
            return None

    def _refresh_lsp_style(self):
        """主题变更时刷新自动诊断文字和所有扩展名后缀颜色"""
        Colors.refresh()
        self._diag_desc.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        for row in self._rows.values():
            if hasattr(row, "refresh_style"):
                try:
                    row.refresh_style()
                except RuntimeError:
                    pass

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

        self._diag_desc = QLabel("自动诊断")
        self._diag_desc.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        self._diag_desc.setToolTip(
            "开启后，write / edit / multi_edit 编辑文件时，"
            "若文件后缀命中已注册 LSP 服务器，自动运行诊断并随编辑结果返回"
        )
        row_layout.addWidget(self._diag_desc)

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
                item.widget().hide()
                item.widget().deleteLater()

        # 更新开关状态（与配置同步）
        self._auto_diag_switch.blockSignals(True)
        self._auto_diag_switch.setChecked(self.cfg.lsp_auto_diagnose.value)
        self._auto_diag_switch.blockSignals(False)

        mgr = self._get_lsp_manager()
        if not mgr or not mgr._clients:
            empty_label = QLabel("暂无 LSP 服务器", self.view)
            empty_label.setStyleSheet(
                f"color: #888; padding: 16px; {get_font_family_css()} font-size: {scale_font_size(12)}px;"
            )
            empty_label.setAlignment(Qt.AlignCenter)
            self.viewLayout.addWidget(empty_label)
        else:
            for name, client in mgr._clients.items():
                exts = list(client.config.extension_to_language.keys())
                # 检查二进制是否可用
                is_installed = client.is_command_available()
                install_hint = "" if is_installed else client.config.install_hint
                row = LspServerRow(
                    name,
                    exts,
                    client.is_running,
                    install_hint=install_hint,
                    parent=self.view,
                )
                row.installRequested.connect(self._on_install_requested)
                row.confirmRequested.connect(self._on_confirm_requested)
                # 门禁待确认态：非内置源（用户级插件）首启被判 need_confirm，
                # 此前无任何确认入口 → 用户自装 LSP 插件永远启不了（EU-G22）
                gate_key = self._gate_key_for(client)
                if gate_key and not client.is_running:
                    row.set_gate_pending(gate_key)
                self._rows[name] = row
                self.viewLayout.addWidget(row)

        # ★ 原实现在这里 QCoreApplication.processEvents()：泵走当时事件队列里的
        # 全部事件，本函数耗时因此变成"那一刻队列里积压了什么"（实测首建被顶到
        # 691ms，其中绝大部分是替别人还债）。布局尺寸不需要它：takeAt 已把 item
        # 摘出布局，sizeHint 不再计入；hide() 保证残留 widget 在被 delete 前不重绘。
        self.viewLayout.activate()
        self.view.updateGeometry()
        self._adjustViewSize()

        if was_expanded:
            h = self.viewLayout.sizeHint().height()
            if h > 0:
                self.setFixedHeight(self.card.height() + h)

        apply_font_size_to_widget(self, 14)

    def _refresh_status(self):
        """定时刷新——配置变化时自动重建列表，否则只更新状态灯

        主线程零 I/O：命令可用性只读缓存（is_command_available_cached），
        实际的 PATH 扫描（shutil.which，同步磁盘 I/O）由后台 daemon 线程预热。
        采样器曾抓到同步版本在主线程阻塞 793ms~2108ms（磁盘 I/O 高压时），
        且拖拽守卫只能推迟——松手瞬间补课照样卡，必须彻底移出主线程。
        """
        mgr = self._get_lsp_manager()
        if not mgr:
            return

        # 检测 LSP 配置是否发生变化（插件热重载增删了服务器）
        current_names = set(mgr._clients.keys())
        card_names = set(self._rows.keys())
        if current_names != card_names:
            self._rebuild()
            return

        # 只更新现有的运行/安装状态（仅读缓存，绝不扫盘）
        need_warm = []
        for name, row in self._rows.items():
            client = mgr._clients.get(name)
            if client:
                row.set_running(client.is_running)
                if not client.is_running:
                    cached = client.is_command_available_cached()
                    if cached is False:
                        row._install_hint = client.config.install_hint
                    need_warm.append(client)

        # 后台预热命令解析缓存（缓存新鲜时该线程几乎零开销即退出）
        if need_warm:
            self._warm_cmd_cache_async(need_warm)

    def _warm_cmd_cache_async(self, clients: list):
        """后台 daemon 线程预热 LSP 命令解析缓存（PATH 扫描不进主线程）"""
        t = getattr(self, "_cmd_warm_thread", None)
        if t is not None and t.is_alive():
            return

        import threading

        def _work():
            for c in clients:
                try:
                    c.is_command_available()  # 命中 TTL 缓存时无 I/O
                except Exception:
                    pass

        self._cmd_warm_thread = threading.Thread(target=_work, name="lsp-cmd-warm", daemon=True)
        self._cmd_warm_thread.start()

    def _gate_key_for(self, client) -> str:
        """该 LSP 服务器若处于门禁待确认态，返回其 gate key；否则空串

        key 口径与 `mcp_lsp_safety.server_key` 一致：`lsp:<plugin>:<server>`。
        判定顺序：先看会话拒绝集合（用户点过拒绝 → 不再提示），再看待确认集合。
        """
        try:
            from app.core.tools.mcp_lsp_safety import (
                is_pending_confirm_by_key,
                is_session_denied,
                server_key,
            )

            cfg = getattr(client, "config", None)
            if cfg is None:
                return ""
            key = server_key("lsp", getattr(cfg, "plugin_name", "") or "", getattr(cfg, "name", "") or "")
            if is_session_denied(key):
                return ""
            return key if is_pending_confirm_by_key(key) else ""
        except Exception as e:  # noqa: BLE001 - 门禁不可用时不阻断列表渲染
            logger.debug(f"[LSP] 门禁待确认态查询失败: {e}")
            return ""

    def _on_confirm_requested(self, gate_key: str, allow: bool):
        """用户在 LSP 行点「放行」→ 写白名单并尝试启动该服务器

        对齐 MCP 侧 `mcp_setting_card.py:911/917` 的 confirm_by_key 用法。
        此前 LSP 侧零调用 → 用户自装插件的 server 永远启不了（EU-G22）。
        """
        if not gate_key:
            return
        try:
            from app.core.tools.mcp_lsp_safety import confirm_by_key

            confirm_by_key(gate_key, allow=allow)
            logger.info(f"[LSP] 用户确认门禁: key={gate_key} allow={allow}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[LSP] 门禁确认写入失败: {e}")
            return
        # 放行后尝试启动（拒绝则仅清掉按钮，由用户手动重试/重载插件）
        if allow:
            self._try_start_after_confirm(gate_key)
        self._rebuild()

    def _try_start_after_confirm(self, gate_key: str) -> None:
        """确认放行后尝试启动对应服务器（失败静默：用户可重载插件手动重试）"""
        try:
            import asyncio

            mgr = self._get_lsp_manager()
            loop = getattr(mgr, "_loop", None) if mgr else None
            if mgr is None or loop is None:
                return
            for client in mgr._clients.values():
                cfg = getattr(client, "config", None)
                name = getattr(cfg, "name", "") or ""
                if name and gate_key.endswith(f":{name}"):
                    asyncio.run_coroutine_threadsafe(client.start(), loop)
                    logger.info(f"[LSP] 门禁放行后已触发启动: {name}")
                    return
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[LSP] 确认后启动尝试失败（可手动重载插件）: {e}")

    def _on_install_requested(self, server_name: str, install_hint: str):
        """处理安装按钮点击 — 在终端中执行安装命令"""
        # InfoBar 统一挂到 tab 管理器顶层窗口（未就绪时兜底卡片所在窗口）
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window()

        if not install_hint:
            InfoBar.warning(
                title="安装命令不可用",
                content=f"LSP 服务器「{server_name}」未提供安装命令。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=bar_parent,
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
            f"即将在终端中执行以下安装命令：\n\n    {_cmd}\n\n安装完成后请点击「刷新」按钮重新连接。\n\n是否继续？",
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
                script = f'tell application "Terminal" to do script "{_cmd}"'
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
                        parent=bar_parent,
                    )
                    return

            InfoBar.success(
                title=f"正在安装 {server_name}",
                content="已在终端中打开安装进程。完成后请点击「刷新」按钮。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=bar_parent,
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
                parent=bar_parent,
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
