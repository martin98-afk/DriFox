# -*- coding: utf-8 -*-
"""
MCP Server 配置卡片

列表卡片 + 编辑卡片，参考 ProviderListSettingCard / ProviderEditCard 模式：
- MCPListSettingCard: 展示服务器列表，含添加/编辑/删除/启停
- MCPEditCard: 编辑/添加服务器的表单卡片（承载在 BaseSettingsCard 中）
"""

import json
import time
from typing import Dict

from loguru import logger
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    ComboBox,
    ExpandSettingCard,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PushButton,
    StrongBodyLabel,
    SwitchButton,
    ToolButton,
)

from app.tools.mcp_tools import MCPState
from app.utils.config import Settings
from app.utils.design_tokens import (
    ButtonStyles,
    CardStyles,
    Colors,
    Sizes,
    SwitchStyles,
    apply_font_size_to_widget,
    scale_font_size,
)
from app.utils.utils import get_font_family_css
from app.widgets.elided_label import _ElidedLabel


class NoWheelComboBox(ComboBox):
    """禁用滚轮切换的普通下拉框（防止误触）"""

    def wheelEvent(self, event):
        event.ignore()


# ═══════════════════════════════════════════════════════════
# 共用表单样式（统一来自 design_tokens）
# ═══════════════════════════════════════════════════════════

EDIT_CARD_STYLE = CardStyles.edit_card_style()


# ═══════════════════════════════════════════════════════════
# 连接状态指示灯配色（四态）
# ═══════════════════════════════════════════════════════════

_STATUS_STYLE = {
    MCPState.CONNECTING: ("#f59e0b", "正在启动..."),
    MCPState.CONNECTED: ("#22c55e", "已连接"),
    MCPState.FAILED: ("#ef4444", "启动失败"),
    MCPState.DISABLED: ("#000000", "已关闭"),
}


def _disabled_dot_color() -> str:
    """ "已关闭"用黑色；暗色主题下纯黑不可见，退化为深灰保证可辨识"""
    try:
        c = QColor(Colors.TEXT_PRIMARY)
        # 主文字偏亮 → 当前是暗色主题
        if c.isValid() and (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) > 140:
            return "#4b5563"
    except Exception:
        pass
    return "#000000"


def _make_row(label_text: str, widget: QWidget, label_width: int = 70) -> QHBoxLayout:
    """构造一行：右对齐标签 + 输入控件"""
    row = QHBoxLayout()
    row.setSpacing(8)
    label = BodyLabel(label_text)
    label.setFixedWidth(label_width)
    label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    row.addWidget(label)
    row.addWidget(widget, 1)
    return row, label


# ═══════════════════════════════════════════════════════════
# MCPEditCard — 添加/编辑 MCP Server 的表单卡片
# ═══════════════════════════════════════════════════════════


class MCPEditCard(QWidget):
    """MCP Server 编辑卡片 — 承载在 BaseSettingsCard 中

    支持两种编辑模式：
    - 表单模式（默认）：分字段填写
    - JSON 模式：直接编辑 JSON 格式配置
    """

    saved = pyqtSignal(dict)
    closed = pyqtSignal()

    def __init__(self, server_data: dict = None, parent=None):
        super().__init__(parent)
        self._server_data = server_data or {}
        self._is_edit = bool(server_data)
        self._original_name = self._server_data.get("name")  # 记录原始名称用于更新定位
        self._json_mode = False
        # 收集所有 QPlainTextEdit 引用用于 refresh_style 时更新 palette
        self._plain_text_edits = []
        self._init_ui()

    def _apply_edit_card_style(self):
        """应用 EDIT_CARD_STYLE 到自身 + 设置 QPlainTextEdit placeholder 颜色"""
        Colors.refresh()
        style = CardStyles.edit_card_style()
        self.setStyleSheet(style)
        # jsonEdit 在 _init_ui 中有独立的 setStyleSheet，需单独刷新
        if hasattr(self, "jsonEdit"):
            self.jsonEdit.setStyleSheet(style)
        # QPlainTextEdit 不支持 CSS ::placeholder，通过 palette 设置
        from PyQt5.QtGui import QColor, QPalette

        ph_color = self._parse_placeholder_color()
        for pte in self._plain_text_edits:
            try:
                p = pte.palette()
                p.setColor(QPalette.PlaceholderText, ph_color)
                pte.setPalette(p)
            except RuntimeError:
                pass

    @staticmethod
    def _parse_placeholder_color() -> "QColor":
        """解析 Colors.INPUT_PLACEHOLDER 为 QColor（兼容 rgba/css 格式）"""
        from PyQt5.QtGui import QColor

        s = Colors.INPUT_PLACEHOLDER.strip()
        if s.startswith("rgba("):
            parts = s[5:-1].split(",")
            r, g, b = int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())
            a = int(float(parts[3].strip()) * 255)
            return QColor(r, g, b, a)
        if s.startswith("rgb("):
            parts = s[4:-1].split(",")
            r, g, b = int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())
            return QColor(r, g, b)
        return QColor(s)

    def refresh_style(self):
        """主题切换时刷新编辑卡片的所有样式"""
        self._apply_edit_card_style()

    def _build_json_preview(self) -> str:
        """从表单构建 JSON 预览文本（标准 mcpServers 格式）"""
        data = self._collect_form_data()
        if not data:
            return ""
        name = data.pop("name", "server")
        srv_type = data.pop("type", "stdio")
        data.pop("enabled", None)  # enabled 是 UI 字段，不输出
        if srv_type != "stdio":
            data["type"] = srv_type
        result = {"mcpServers": {name: data}}
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _build_json_from_data(self) -> str:
        """从已有 server_data 构建 JSON（标准 mcpServers 格式）"""
        data = dict(self._server_data)
        name = data.pop("name", "my-server")
        # 去掉内部字段
        enabled = data.pop("enabled", True)
        server_type = data.pop("type", "stdio")
        # 如果是 stdio，type 不输出（标准格式默认 stdio）
        # 如果是 sse/http，输出 url/headers 标准结构
        if server_type != "stdio":
            data["type"] = server_type
        # 组装 mcpServers 格式
        result = {"mcpServers": {name: data}}
        return json.dumps(result, indent=2, ensure_ascii=False)

    # 模式切换信号（通知外层更新头部按钮）
    modeChanged = pyqtSignal(bool)  # True=JSON模式, False=表单模式

    def _init_ui(self):
        self._apply_edit_card_style()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 2, 4, 2)
        main_layout.setSpacing(6)

        # ── QStackedWidget：表单页(0) / JSON页(1) ──
        self._stack = QStackedWidget()
        main_layout.addWidget(self._stack, 1)

        # ── 表单页 ──
        self._form_page = QWidget()
        self._form_page.setStyleSheet("background: transparent;")
        form_layout = QVBoxLayout(self._form_page)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(6)
        self._form_layout = form_layout

        # ── 名称 ──
        self.nameEdit = QLineEdit()
        self.nameEdit.setPlaceholderText("例如: github, filesystem, my-api")
        if self._is_edit:
            self.nameEdit.setText(self._server_data.get("name", ""))
        row, _ = _make_row("名称:", self.nameEdit)
        form_layout.addLayout(row)

        # ── 类型 ──
        self.typeCombo = NoWheelComboBox()
        self.typeCombo.addItems(["stdio", "sse", "http"])
        self.typeCombo.setCurrentText(self._server_data.get("type", "stdio"))
        self.typeCombo.currentTextChanged.connect(self._on_type_changed)
        row, _ = _make_row("类型:", self.typeCombo)
        form_layout.addLayout(row)

        # ── Command（stdio） ──
        self.commandEdit = QLineEdit()
        self.commandEdit.setPlaceholderText("例如: npx")
        self.commandEdit.setText(self._server_data.get("command", ""))
        self._cmd_row, self._cmd_label = _make_row("Command:", self.commandEdit)
        form_layout.addLayout(self._cmd_row)

        # ── Args（stdio） ──
        self.argsEdit = QLineEdit()
        self.argsEdit.setPlaceholderText("例如: -y @modelcontextprotocol/server-filesystem /path")
        saved_args = self._server_data.get("args", [])
        if isinstance(saved_args, list):
            self.argsEdit.setText(" ".join(saved_args))
        self._args_row, self._args_label = _make_row("Args:", self.argsEdit)
        form_layout.addLayout(self._args_row)

        # ── URL（sse/http） ──
        self.urlEdit = QLineEdit()
        self.urlEdit.setPlaceholderText("例如: https://api.example.com/mcp")
        self.urlEdit.setText(self._server_data.get("url", ""))
        self._url_row, self._url_label = _make_row("URL:", self.urlEdit)
        form_layout.addLayout(self._url_row)

        # ── Headers（sse/http） ──
        self.headersEdit = QPlainTextEdit()
        self._plain_text_edits.append(self.headersEdit)
        self.headersEdit.setMaximumHeight(100)
        self.headersEdit.setMinimumHeight(48)
        self.headersEdit.setPlaceholderText('可选 JSON，例如: {"Authorization": "Bearer xxx"}')
        saved_headers = self._server_data.get("headers")
        if saved_headers and isinstance(saved_headers, dict):
            self.headersEdit.setPlainText(json.dumps(saved_headers, indent=2, ensure_ascii=False))
        self._headers_row, self._headers_label = _make_row("Headers:", self.headersEdit)
        form_layout.addLayout(self._headers_row)

        # ── 环境变量（stdio） ──
        self.envEdit = QPlainTextEdit()
        self._plain_text_edits.append(self.envEdit)
        self.envEdit.setMinimumHeight(48)
        self.envEdit.setPlaceholderText('可选 JSON，例如: {"API_KEY": "xxx"}')
        saved_env = self._server_data.get("env")
        if saved_env and isinstance(saved_env, dict):
            self.envEdit.setPlainText(json.dumps(saved_env, indent=2, ensure_ascii=False))
        self._env_row, self._env_label = _make_row("环境变量:", self.envEdit)
        form_layout.addLayout(self._env_row)

        # 表单页加入 stack 索引 0
        self._stack.addWidget(self._form_page)

        # ── JSON 页 ──
        self._json_page = QWidget()
        self._json_page.setStyleSheet("background: transparent;")
        json_layout = QVBoxLayout(self._json_page)
        json_layout.setContentsMargins(0, 0, 0, 0)
        self.jsonEdit = QPlainTextEdit()
        self._plain_text_edits.append(self.jsonEdit)
        self.jsonEdit.setStyleSheet(EDIT_CARD_STYLE)
        self.jsonEdit.setPlaceholderText(
            "粘贴标准 MCP 配置（支持两种格式）:\n\n"
            "【格式一】Claude Desktop / Cursor 标准格式:\n"
            "{\n"
            '  "mcpServers": {\n'
            '    "brave-search": {\n'
            '      "command": "npx",\n'
            '      "args": ["-y", "@brave/brave-search-mcp-server"],\n'
            '      "env": {"BRAVE_API_KEY": "xxx"}\n'
            "    }\n"
            "  }\n"
            "}\n\n"
            "【格式二】简化单服务器格式:\n"
            "{\n"
            '  "name": "my-server",\n'
            '  "command": "npx",\n'
            '  "args": ["-y", "some-package"],\n'
            '  "env": {"KEY": "value"}\n'
            "}\n\n"
            '💡 提示: args 含 "--transport http/sse" 时，系统会自动设置连接类型为 http'
        )
        json_data = self._build_json_from_data() if self._server_data else ""
        if json_data:
            self.jsonEdit.setPlainText(json_data)
        json_layout.addWidget(self.jsonEdit)
        # JSON 页加入 stack 索引 1
        self._stack.addWidget(self._json_page)

        # 初始显隐：编辑已有服务器时默认进入 JSON 模式
        # （表单字段不易配置，用户更倾向直接编辑 JSON）；新增服务器默认表单模式
        self._json_mode = self._is_edit
        self._stack.setCurrentIndex(1 if self._is_edit else 0)
        self._on_type_changed(self.typeCombo.currentText())

    def _toggle_mode(self):
        """切换表单/JSON 编辑模式"""
        # 切到 JSON 模式
        if not self._json_mode:
            self._json_mode = True
            # 同步表单数据到 JSON 编辑器（标准格式）
            form_data = self._collect_form_data()
            if form_data:
                preview = self._build_json_preview()
                self.jsonEdit.setPlainText(preview if preview else json.dumps(form_data, indent=2, ensure_ascii=False))
            self._stack.setCurrentIndex(1)
        # 切回表单模式
        else:
            json_text = self.jsonEdit.toPlainText().strip()
            if json_text:
                try:
                    parsed = json.loads(json_text)
                    parsed = self._parse_mcp_json(parsed)
                    self._apply_json_to_form(parsed)
                except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
                    from app.widgets.tab_manager_window import TabManagerWindow

                    InfoBar.warning(
                        "提示",
                        f"JSON 解析失败: {e}，无法切换回表单模式",
                        parent=TabManagerWindow.get_instance() or self.window(),
                        duration=3000,
                        position=InfoBarPosition.BOTTOM,
                    )
                    self._json_mode = True  # 保持 JSON 模式
                    self._stack.setCurrentIndex(1)
                    return
            self._json_mode = False
            # 同步字段显隐（按当前类型显示/隐藏 cmd/args/url/headers/env，
            # 并让 http/sse 的 headers 拉伸填满剩余空间）
            self._on_type_changed(self.typeCombo.currentText())
            self._stack.setCurrentIndex(0)
        self.modeChanged.emit(self._json_mode)

    def _collect_form_data(self) -> dict:
        """收集表单当前值，返回 dict"""
        name = self.nameEdit.text().strip()
        if not name:
            return None
        server_type = self.typeCombo.currentText()
        data = {
            "name": name,
            "type": server_type,
            "enabled": self._server_data.get("enabled", True),
        }
        if server_type == "stdio":
            cmd = self.commandEdit.text().strip()
            if not cmd:
                return None
            data["command"] = cmd
            args_text = self.argsEdit.text().strip()
            data["args"] = args_text.split() if args_text else []
            env_text = self.envEdit.toPlainText().strip()
            if env_text:
                try:
                    data["env"] = json.loads(env_text)
                except json.JSONDecodeError:
                    return None
        else:
            url = self.urlEdit.text().strip()
            if not url:
                return None
            data["url"] = url
            headers_text = self.headersEdit.toPlainText().strip()
            if headers_text:
                try:
                    data["headers"] = json.loads(headers_text)
                except json.JSONDecodeError:
                    return None
            else:
                data["headers"] = {}
        return data

    def _parse_mcp_json(self, parsed: dict) -> dict:
        """
        将 JSON 格式转为内部 server_data 格式。
        支持输入：
        - 标准 mcpServers 格式: {"mcpServers": {"name": {...}}}
        - 简化格式: {"name": "...", "command": "...", ...}
        返回: {"name": "...", "type": "...", "command": "...", ...}

        自动检测：
        - args 含 --transport http → 自动设 type=http 并提示
        """
        # 格式一：mcpServers 包裹格式
        if "mcpServers" in parsed:
            servers_dict = parsed["mcpServers"]
            if not isinstance(servers_dict, dict) or not servers_dict:
                raise ValueError("mcpServers 必须包含至少一个服务器")
            # 取第一个服务器
            server_name = next(iter(servers_dict))
            server_cfg = servers_dict[server_name]
            if not isinstance(server_cfg, dict):
                raise ValueError(f"服务器 '{server_name}' 配置格式错误")
            result = {"name": server_name}
            result.update(server_cfg)
            return self._normalize_server_data(result)

        # 格式二：简化格式（平铺键值）
        if "name" not in parsed:
            raise ValueError("缺少 'name' 字段或 'mcpServers' 包裹")
        return self._normalize_server_data(dict(parsed))

    def _normalize_server_data(self, data: dict) -> dict:
        """规范化 server_data，补充缺失字段、检测类型"""
        if "enabled" not in data:
            data["enabled"] = True

        # 自动检测类型（仅在 JSON 未显式声明 type 时）
        # 优先级：command 存在 → stdio（stdio 可并存 url/env 等字段，command 优先）；
        #         其次 args 显式声明 --transport http/sse → 对应类型；
        #         其次存在 url → sse（默认）；否则 → stdio。
        # 注意：不能仅凭 url 判定为 sse，否则一个带 url 字段的 stdio 服务器
        # （常见于模板/复制配置）会被误判成 sse。
        if "type" not in data:
            args = data.get("args", []) or []
            has_http_transport = any(
                isinstance(a, str) and "--transport" in a and i + 1 < len(args) and args[i + 1] in ("http", "sse")
                for i, a in enumerate(args)
            )
            if data.get("command"):
                data["type"] = "stdio"
            elif has_http_transport:
                data["type"] = "http"
            elif "url" in data:
                data["type"] = "sse"
            else:
                data["type"] = "stdio"
        return data

    def _apply_json_to_form(self, parsed: dict):
        """将解析后的 JSON dict 写回表单字段"""
        self.nameEdit.setText(parsed.get("name", ""))
        if self._is_edit:
            self.nameEdit.setReadOnly(True)

        srv_type = parsed.get("type", "stdio")
        idx = self.typeCombo.findText(srv_type)
        if idx >= 0:
            self.typeCombo.setCurrentIndex(idx)

        self.commandEdit.setText(parsed.get("command", ""))
        args = parsed.get("args", [])
        self.argsEdit.setText(" ".join(args) if isinstance(args, list) else "")
        self.urlEdit.setText(parsed.get("url", ""))
        headers = parsed.get("headers")
        if headers and isinstance(headers, dict):
            self.headersEdit.setPlainText(json.dumps(headers, indent=2, ensure_ascii=False))
        else:
            self.headersEdit.clear()
        env = parsed.get("env")
        if env and isinstance(env, dict):
            self.envEdit.setPlainText(json.dumps(env, indent=2, ensure_ascii=False))
        else:
            self.envEdit.clear()

    def _on_type_changed(self, server_type: str):
        if self._json_mode:
            return  # JSON 模式下不处理字段显隐
        is_stdio = server_type == "stdio"
        for w in (self._cmd_label, self.commandEdit):
            w.setVisible(is_stdio)
        for w in (self._args_label, self.argsEdit):
            w.setVisible(is_stdio)
        for w in (self._url_label, self.urlEdit):
            w.setVisible(not is_stdio)
        for w in (self._headers_label, self.headersEdit):
            w.setVisible(not is_stdio)
        for w in (self._env_label, self.envEdit):
            w.setVisible(is_stdio)

        # http/sse 类型：headers 输入框拉伸填满剩余纵向空间（不再受 100px 上限约束）
        if not is_stdio:
            self._form_layout.setStretchFactor(self._headers_row, 1)
            self.headersEdit.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX，取消上限
            self.headersEdit.setMinimumHeight(140)
            self.headersEdit.setSizePolicy(QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding))
        else:
            self._form_layout.setStretchFactor(self._headers_row, 0)
            self.headersEdit.setMaximumHeight(100)
            self.headersEdit.setMinimumHeight(48)
            self.headersEdit.setSizePolicy(QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred))

    def _on_save(self):
        from app.widgets.tab_manager_window import TabManagerWindow

        _parent = TabManagerWindow.get_instance() or self.window()
        if self._json_mode:
            # JSON 模式：支持标准 mcpServers 格式和简化格式
            json_text = self.jsonEdit.toPlainText().strip()
            if not json_text:
                InfoBar.warning(
                    "提示", "请输入 JSON 配置", parent=_parent, duration=2000, position=InfoBarPosition.BOTTOM
                )
                return
            try:
                parsed = json.loads(json_text)
            except json.JSONDecodeError as e:
                InfoBar.warning(
                    "提示", f"JSON 格式错误: {e}", parent=_parent, duration=3000, position=InfoBarPosition.BOTTOM
                )
                return
            try:
                server_data = self._parse_mcp_json(parsed)
                # 保留来源信息（从原始数据继承，供 PluginManager 更新使用）
                if self._original_name:
                    server_data["_source"] = self._original_name
            except (ValueError, KeyError, TypeError) as e:
                InfoBar.warning(
                    "提示", f"配置解析失败: {e}", parent=_parent, duration=3000, position=InfoBarPosition.BOTTOM
                )
                return
            self.saved.emit(server_data)
            return

        # 表单模式
        name = self.nameEdit.text().strip()
        if not name:
            InfoBar.warning("提示", "请输入服务器名称", parent=_parent, duration=2000, position=InfoBarPosition.BOTTOM)
            return

        server_type = self.typeCombo.currentText()
        server_data = {
            "name": name,
            "type": server_type,
            "enabled": self._server_data.get("enabled", True),
        }

        if server_type == "stdio":
            cmd = self.commandEdit.text().strip()
            if not cmd:
                InfoBar.warning(
                    "提示", "请输入 Command", parent=_parent, duration=2000, position=InfoBarPosition.BOTTOM
                )
                return
            server_data["command"] = cmd
            args_text = self.argsEdit.text().strip()
            server_data["args"] = args_text.split() if args_text else []

            env_text = self.envEdit.toPlainText().strip()
            if env_text:
                try:
                    server_data["env"] = json.loads(env_text)
                except json.JSONDecodeError as e:
                    InfoBar.warning(
                        "提示",
                        f"环境变量 JSON 格式错误: {e}",
                        parent=_parent,
                        duration=3000,
                        position=InfoBarPosition.BOTTOM,
                    )
                    return
        else:
            url = self.urlEdit.text().strip()
            if not url:
                InfoBar.warning("提示", "请输入 URL", parent=_parent, duration=2000, position=InfoBarPosition.BOTTOM)
                return
            server_data["url"] = url
            headers_text = self.headersEdit.toPlainText().strip()
            if headers_text:
                try:
                    server_data["headers"] = json.loads(headers_text)
                except json.JSONDecodeError as e:
                    InfoBar.warning(
                        "提示",
                        f"Headers JSON 格式错误: {e}",
                        parent=_parent,
                        duration=3000,
                        position=InfoBarPosition.BOTTOM,
                    )
                    return
            else:
                server_data["headers"] = {}

        # 保留来源信息（供 PluginManager.update_mcp_server 定位文件）
        if self._original_name:
            server_data["_source"] = self._original_name

        self.saved.emit(server_data)


# ═══════════════════════════════════════════════════════════
# MCPServerRow — 列表中的单行
# ═══════════════════════════════════════════════════════════


class MCPServerRow(CardWidget):
    """单行 MCP Server 显示"""

    removeRequested = pyqtSignal(str)
    editRequested = pyqtSignal(str)
    enabledChanged = pyqtSignal(str, bool)

    def __init__(self, server_data: dict, parent=None):
        super().__init__(parent)
        self._name = server_data.get("name", "")
        self._setup_ui(server_data)

    def set_enabled(self, enabled: bool):
        """外部更新开关状态（避免全量刷新）"""
        self.switch.blockSignals(True)
        self.switch.setChecked(enabled)
        self.switch.blockSignals(False)

    def set_status(self, state: str, error: str = "", tool_count: int = 0):
        """更新连接状态指示灯（四态）

        启动中 → 黄色，启动失败 → 红色，已关闭 → 黑色，成功 → 绿色。

        Args:
            state: MCPState 之一（connecting / connected / failed / disabled）
            error: 失败原因（作为 tooltip 展示）
            tool_count: 已发现的工具数量
        """
        color, tip = _STATUS_STYLE.get(state, _STATUS_STYLE[MCPState.DISABLED])
        if state == MCPState.DISABLED:
            color = _disabled_dot_color()
        elif state == MCPState.CONNECTED and tool_count:
            tip = f"已连接 · {tool_count} 个工具"
        elif state == MCPState.FAILED and error:
            tip = error if len(error) <= 400 else error[:400] + "..."

        self._current_state = state
        self._status_dot.setText("●")
        self._status_dot.setStyleSheet(
            f"color: {color}; font-size: {scale_font_size(16)}px; background: transparent; padding: 0;"
        )
        self._status_dot.setToolTip(tip)

    def refresh_style(self):
        """主题变更时刷新命令描述文字颜色 + 指示灯（黑色需随明暗主题调整）"""
        Colors.refresh()
        self._desc_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} font-size: {scale_font_size(12)}px;"
        )
        if getattr(self, "_current_state", None) == MCPState.DISABLED:
            self._status_dot.setStyleSheet(
                f"color: {_disabled_dot_color()}; font-size: {scale_font_size(16)}px;"
                f" background: transparent; padding: 0;"
            )

    def _setup_ui(self, data: dict):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)

        # 连接状态指示灯（黄=启动中 / 绿=成功 / 红=失败 / 黑=关闭）
        self._current_state = MCPState.DISABLED
        self._status_dot = QLabel("●")
        self._status_dot.setFixedWidth(16)
        self._status_dot.setAlignment(Qt.AlignCenter)
        self._status_dot.setToolTip("已关闭")
        self._status_dot.setStyleSheet(
            f"color: {_disabled_dot_color()}; font-size: {scale_font_size(16)}px; background: transparent; padding: 0;"
        )
        layout.addWidget(self._status_dot)

        name_label = StrongBodyLabel(data.get("name", ""))
        name_label.setFixedWidth(100)
        layout.addWidget(name_label)
        server_type = data.get("type", "stdio")
        if server_type == "stdio":
            desc = f"{data.get('command', '')} {' '.join(data.get('args', []))}".strip()
        else:
            desc = data.get("url", "")
        self._desc_label = _ElidedLabel(desc)
        self._desc_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} font-size: {scale_font_size(12)}px;"
        )
        self._desc_label.setMinimumWidth(40)
        layout.addWidget(self._desc_label, 1)

        self.switch = SwitchButton()
        SwitchStyles.configure(self.switch)
        self.switch.setChecked(data.get("enabled", True))
        self.switch.checkedChanged.connect(lambda v: self.enabledChanged.emit(self._name, v))
        layout.addWidget(self.switch)

        edit_btn = ToolButton(FluentIcon.EDIT)
        edit_btn.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        edit_btn.setStyleSheet(ButtonStyles.tool_button())
        edit_btn.clicked.connect(lambda: self.editRequested.emit(self._name))
        layout.addWidget(edit_btn)

        del_btn = ToolButton(FluentIcon.CLOSE)
        del_btn.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        del_btn.setStyleSheet(ButtonStyles.tool_button())
        del_btn.clicked.connect(lambda: self.removeRequested.emit(self._name))
        layout.addWidget(del_btn)


# ═══════════════════════════════════════════════════════════
# MCPListSettingCard — MCP Server 列表设置卡片
# ═══════════════════════════════════════════════════════════


class MCPListSettingCard(ExpandSettingCard):
    """MCP Server 管理设置卡片"""

    serversChanged = pyqtSignal()
    showAddCard = pyqtSignal()
    showEditCard = pyqtSignal(str, dict)

    # 内部信号（从后台线程桥接到主线程 UI 更新）
    _hotConnectResult = pyqtSignal(str, bool, str)
    _tokenCountReady = pyqtSignal(str)

    def __init__(self, icon, title: str, content: str = None, parent=None):
        self.cfg = Settings.get_instance()
        # 必须在任何可能触发 _update_mcp_token_count 的路径之前初始化，
        # 否则 _status_timer(3s) 首次触发时若 __init__ 中途异常被吞，会抛
        # AttributeError 并惊动主线程异常钩子做昂贵 traceback 格式化（拖拽卡顿源之一）。
        self._token_calc_running = False
        super().__init__(icon, title, content, parent)
        # 防抖节流：300ms 滚动防抖，等待用户停止操作
        self._switch_debounce_timer = QTimer(self)
        self._switch_debounce_timer.setSingleShot(True)
        self._switch_debounce_timer.setInterval(300)
        self._switch_debounce_timer.timeout.connect(self._do_debounced_switch)
        self._switch_debounce_timer.timeout.connect(self._do_debounced_global_switch)
        # 每个 server name 只保留最后一次目标状态
        self._pending_server_switches: Dict[str, bool] = {}
        self._pending_global_switch: bool = True
        self._global_switch_pending = False
        # 行引用（用于开关操作时直接更新，避免全量刷新）
        self._server_rows: Dict[str, "MCPServerRow"] = {}
        # 自触发抑制：本卡片的开关操作不触发 watchfiles 热重载回刷。
        # 用「过期时间戳」而非布尔：全局开关写入的是 settings（不触发插件热重载），
        # 若用布尔标记会永久停在 True，导致此后所有热重载刷新被吞掉（计数更新、列表不更新）。
        # 窗口取 3s（> watchfiles 的 2s 防抖），既能在自触发刷新到达时正确抑制，又会自动过期。
        self._suppress_hot_reload_until = 0.0
        # 状态轮询定时器（3秒刷新一次连接状态指示灯 + token 占用）
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(3000)
        self._status_timer.timeout.connect(self._refresh_status_dots)
        self._status_timer.timeout.connect(self._update_mcp_token_count)

        self._setup_ui()
        self._refresh()
        # 初始刷新一次状态指示灯
        QTimer.singleShot(500, self._refresh_status_dots)

        # 连接信号（主线程处理 UI）
        self._hotConnectResult.connect(self._on_hot_connect_result)
        self._tokenCountReady.connect(self.setCount)
        # token 估算后台线程运行标志（避免重复启动）
        self._token_calc_running = False

    def _get_pm(self):
        """获取 PluginManager 实例"""
        from app.core.plugin_manager import PluginManager

        return PluginManager.get_instance()

    def _get_servers(self) -> list:
        """获取 MCP 服务器列表（从 PluginManager）"""
        pm = self._get_pm()
        if pm.is_initialized():
            return pm.get_mcp_servers()
        return []

    def _get_mcp_manager(self):
        from app.tools.mcp_tools import MCPClientManager

        return MCPClientManager.get_instance()

    def _setup_ui(self):
        self.viewLayout.setSpacing(2)
        self.viewLayout.setAlignment(Qt.AlignTop)
        self.viewLayout.setContentsMargins(8, 0, 8, 0)
        self.view.setStyleSheet("background-color: transparent;")

        self.addButton = PushButton("添加", self, FluentIcon.ADD)
        self.addButton.clicked.connect(self.showAddCard.emit)
        self.addWidget(self.addButton)

        self.globalSwitch = SwitchButton()
        self.globalSwitch.setChecked(self.cfg.mcp_enabled.value)
        SwitchStyles.configure(self.globalSwitch)
        self.globalSwitch.checkedChanged.connect(self._on_global_switch)
        self.addWidget(self.globalSwitch)

        self._update_button_position()

    def _update_button_position(self):
        """将 addButton + globalSwitch 移到卡片头部 expandButton 左侧"""
        card = self.card
        if not hasattr(card, "hBoxLayout"):
            return
        # 先从原始位置移除
        card.hBoxLayout.removeWidget(self.addButton)
        card.hBoxLayout.removeWidget(self.globalSwitch)
        # 找到 expandButton 位置，在其前面插入
        for i in range(card.hBoxLayout.count()):
            item = card.hBoxLayout.itemAt(i)
            if item.widget() == card.expandButton:
                card.hBoxLayout.removeItem(card.hBoxLayout.itemAt(i - 1))
                card.hBoxLayout.insertWidget(i - 1, self.globalSwitch, 0, Qt.AlignRight)
                card.hBoxLayout.insertSpacing(i - 1, 4)
                card.hBoxLayout.insertWidget(i - 1, self.addButton, 0, Qt.AlignRight)
                card.hBoxLayout.insertSpacing(i - 1, 4)
                card.hBoxLayout.insertSpacing(i + 3, 4)
                break

    # ── 热更新操作（全部后台，不阻塞 UI）────────────

    def _hot_connect(self, name: str, config: dict, force: bool = False):
        """后台连接单个服务器（不阻塞 UI）

        Args:
            name: 服务器名称
            config: 服务器配置
            force: 是否强制重连（跳过已连接检查，用于开关切换时同步状态）
        """
        mgr = self._get_mcp_manager()
        if not self.cfg.mcp_enabled.value:
            return

        # 防重复：已连接的不再触发（除非 force=True）
        if not force:
            status_list = mgr.get_status()
            already = any(st["name"] == name and st["connected"] for st in status_list)
            if already:
                logger.debug(f"[MCP] '{name}' 已连接，跳过热连接")
                return

        def on_done(n, success, error_msg=""):
            self._hotConnectResult.emit(n, success, error_msg)

        mgr.connect_server_background(name, config, on_done=on_done)

    def _on_hot_connect_result(self, name: str, success: bool, error_msg: str = ""):
        """连接结果回调（主线程，可安全操作 UI）"""
        # 立即刷新对应行的状态指示灯
        self._refresh_status_dots()
        if success:
            logger.info(f"[MCP] '{name}' 热连接成功")
        else:
            from app.widgets.tab_manager_window import TabManagerWindow

            _parent = TabManagerWindow.get_instance() or self.window()
            # 提取友好提示
            hint = error_msg or "未知错误"
            if hint == "服务器正在操作中，请稍后重试":
                # 防重丢弃：另一入口正在连接同一服务器，属正常竞态抑制，
                # 不是失败，不弹错误提示（否则热重载+开关并发时频繁误报）
                logger.debug(f"[MCP] '{name}' 连接请求被防重丢弃（已有连接在进行中）")
                return
            if "请检查配置类型是否正确" in hint:
                # 拆分为标题和内容
                parts = hint.split("（", 1)
                display_msg = parts[0]
                detail = "（" + parts[1] if len(parts) > 1 else ""
                logger.warning(f"[MCP] '{name}' 热连接失败: {display_msg}")
                InfoBar.error(
                    title=f"MCP 连接失败: {name}",
                    content=f"{display_msg}\n{detail}" if detail else display_msg,
                    parent=_parent,
                    duration=8000,
                    position=InfoBarPosition.BOTTOM,
                )
            elif hint != "未知错误":
                logger.warning(f"[MCP] '{name}' 热连接失败: {hint}")
                InfoBar.error(
                    title=f"MCP 连接失败: {name}",
                    content=hint,
                    parent=_parent,
                    duration=6000,
                    position=InfoBarPosition.BOTTOM,
                )
            else:
                logger.warning(f"[MCP] '{name}' 热连接失败")
                InfoBar.error(
                    title="MCP 连接失败",
                    content=f"'{name}' 连接失败，请检查配置是否正确",
                    parent=_parent,
                    duration=5000,
                    position=InfoBarPosition.BOTTOM,
                )
        # 注意：连接结果不触发全量刷新——状态指示灯已由 _refresh_status_dots() 更新，
        # token 占用由 3s 状态轮询兜底。全量重建列表会导致开关 MCP 时整卡闪烁。

    def _hot_disconnect(self, name: str):
        """后台断开单个服务器"""
        mgr = self._get_mcp_manager()
        mgr.disconnect_server_background(name)

    def _hot_disconnect_all(self):
        """后台断开所有服务器"""
        mgr = self._get_mcp_manager()
        mgr.disconnect_all_background()
        logger.info("[MCP] 后台断开所有服务器中...")

    # ── 全局开关 ──────────────────────────────────────

    def _on_global_switch(self, enabled: bool):
        """全局开关（滚动防抖）

        只保留最后一次状态；300ms 无新操作才执行。
        """
        self._pending_global_switch = enabled
        self._global_switch_pending = True
        self._switch_debounce_timer.start()

    def _do_debounced_global_switch(self):
        """防抖到期：执行最终的全局开关状态"""
        if not self._global_switch_pending:
            return
        self._global_switch_pending = False

        enabled = self._pending_global_switch

        # 标记：接下来的写文件操作是自触发的，抑制热重载回刷（3s 内有效，自动过期）
        self._suppress_hot_reload_until = time.time() + 3.0

        self.cfg.set(self.cfg.mcp_enabled, enabled, save=True)
        if enabled:
            servers = self._get_servers()
            for s in servers:
                if s.get("enabled", True):
                    self._hot_connect(s.get("name", ""), s)
        else:
            self._hot_disconnect_all()
        self._refresh_status_dots()
        self._update_mcp_token_count()
        # 注意：全局开关不触发全量刷新——列表内容未变，行级状态已由上面的
        # _refresh_status_dots() 更新。全量重建会导致开关时整卡闪烁。

    def consume_hot_reload(self) -> bool:
        """检查并消费自触发标记。热重载触发时调用，返回 True 表示本次是自触发的，应跳过刷新

        采用过期时间戳而非布尔：在窗口内（默认 3s）的自触发刷新会被抑制，
        窗口过后自动失效，避免布尔标记卡死吞掉后续外部热重载。
        """
        now = time.time()
        if now < self._suppress_hot_reload_until:
            self._suppress_hot_reload_until = 0.0
            return True
        self._suppress_hot_reload_until = 0.0
        return False

    # ── 列表刷新 ──────────────────────────────────────

    def _refresh_status_dots(self):
        """刷新所有行的连接状态指示灯"""
        # 拖拽守卫：拖拽期间跳过本轮刷新，避免主线程 UI 更新加剧掉帧
        from app.utils.window_drag_state import any_window_dragging

        if any_window_dragging:
            return
        try:
            mgr = self._get_mcp_manager()
            if not mgr:
                return
            status_list = mgr.get_status()
            status_map = {s["name"]: s for s in status_list}
            # 配置里被关掉的 server 一律显示"已关闭"，即使管理器里还留着旧记录
            cfg_enabled = {s.get("name", ""): s.get("enabled", True) for s in self._get_servers()}
            global_on = self.cfg.mcp_enabled.value

            for name, row in list(self._server_rows.items()):
                if row is None:
                    continue
                try:
                    st = status_map.get(name)
                    if not global_on or not cfg_enabled.get(name, True):
                        # 用户主动关闭（全局或单个）→ 黑色
                        row.set_status(MCPState.DISABLED)
                    elif st is None:
                        # 已启用但管理器还没有任何记录 → 尚未开始启动
                        row.set_status(MCPState.DISABLED)
                    else:
                        row.set_status(
                            st.get("state", MCPState.DISABLED),
                            st.get("error", ""),
                            st.get("tool_count", 0),
                        )
                except RuntimeError:
                    # widget 已被销毁
                    self._server_rows.pop(name, None)
        except Exception:
            logger.debug("[MCP] 刷新状态指示灯异常（正常，卡片初始化时暂无可读状态）")

    def _refresh(self):
        """刷新服务器列表（保留展开状态）"""
        was_expanded = self.isExpand
        # 清除旧的行引用
        self._server_rows.clear()
        # 停止状态轮询（重建列表期间避免操作已销毁的 widget）
        self._status_timer.stop()

        # 稳妥方式清空 viewLayout：takeAt + 删除 widget
        while self.viewLayout.count():
            item = self.viewLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        servers = self._get_servers()
        if not servers:
            empty_label = QLabel("暂无 MCP 服务器，点击「添加服务器」创建", self.view)
            empty_label.setStyleSheet(
                f"color: #888; padding: 16px; {get_font_family_css()} font-size: {scale_font_size(12)}px;"
            )
            empty_label.setAlignment(Qt.AlignCenter)
            self.viewLayout.addWidget(empty_label)
        else:
            for server_data in servers:
                row = MCPServerRow(server_data, self.view)
                row.removeRequested.connect(self._on_remove_server)
                row.editRequested.connect(self._show_edit_dialog)
                row.enabledChanged.connect(self._on_enabled_changed)
                self._server_rows[server_data.get("name", "")] = row
                self.viewLayout.addWidget(row)

        # 处理异步删除（deleteLater）+ 强制布局计算，确保 sizeHint 正确
        from PyQt5.QtCore import QCoreApplication

        QCoreApplication.processEvents()
        self.viewLayout.activate()
        self.view.updateGeometry()

        # 调整展开区域高度
        self._adjustViewSize()
        # 恢复展开状态：已展开时强制刷新高度
        if was_expanded:
            h = self.viewLayout.sizeHint().height()
            if h > 0:
                self.setFixedHeight(self.card.height() + h)

        # 刷新状态指示灯
        self._refresh_status_dots()
        # 启动状态轮询（卡片展开时持续刷新）
        self._status_timer.start()

        # 更新头部 subtitle（服务器计数 + token 占用）
        self._update_mcp_token_count()

        # 重要：新创建的行/标签未应用字体大小，需要重新刷新
        # 否则会回退到 qfluentwidgets 默认的 14px 硬编码字体
        apply_font_size_to_widget(self, 14)

    def refresh_style(self):
        """主题变更时刷新所有行的命令描述颜色"""
        Colors.refresh()
        for row in self._server_rows.values():
            if row is not None and hasattr(row, "refresh_style"):
                try:
                    row.refresh_style()
                except RuntimeError:
                    pass

    def setCount(self, text: str):
        card = self.card
        if hasattr(card, "contentLabel"):
            card.contentLabel.setText(text)

    def _save_servers(self, servers: list):
        """保存服务器列表（底层写入 PluginManager）"""
        # 不再直接写 Settings.mcp_servers，而是通过 PluginManager 管理
        # 此方法保留为空，实际增删改走 PluginManager 的方法
        self._refresh()
        self.serversChanged.emit()

    def _on_remove_server(self, name: str):
        from app.widgets.common_dialogs import ConfirmDialog

        _confirmed: list[bool] = [False]

        def _on_confirm():
            _confirmed[0] = True

        dialog = ConfirmDialog(
            title="删除 MCP 服务器",
            content=f"确定要删除 MCP 服务器「{name}」吗？\n删除后将不再出现在列表中。",
            confirm_text="删除",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(_on_confirm)
        dialog.exec_()
        if _confirmed[0]:
            self._do_remove(name)

    def _do_remove(self, name: str):
        # 热断开
        self._hot_disconnect(name)
        pm = self._get_pm()
        pm.remove_mcp_server(name)
        # 列表内容已变化，这里自行全量刷新即可（serversChanged 广播已不再触发全量重建）
        self._refresh()

    def _show_edit_dialog(self, name: str):
        servers = self._get_servers()
        server_data = next((s for s in servers if s.get("name") == name), None)
        if server_data:
            self.showEditCard.emit(name, server_data)

    def _on_enabled_changed(self, name: str, enabled: bool):
        """单个服务器开关变化（滚动防抖）

        同一 name 只保留最后一次状态；300ms 无新操作才执行。
        """
        self._pending_server_switches[name] = enabled
        # 重启定时器：快速点击会不断重置，直到安静 300ms
        self._switch_debounce_timer.start()

    def _do_debounced_switch(self):
        """防抖到期：按最终状态批量更新开关状态（不触发全量刷新）"""
        if not self._pending_server_switches:
            return
        tasks = dict(self._pending_server_switches)
        self._pending_server_switches.clear()

        # 标记：接下来的写文件操作是自触发的，抑制热重载回刷（3s 内有效，自动过期）
        self._suppress_hot_reload_until = time.time() + 3.0

        # 批量更新配置（通过 PluginManager 更新 enabled 状态）
        servers = self._get_servers()
        pm = self._get_pm()
        for name, enabled in tasks.items():
            server_data = next((s for s in servers if s.get("name") == name), None)
            if server_data:
                server_data["enabled"] = enabled
                pm.update_mcp_server(name, server_data)

        # 执行热连接/断开，并直接更新对应行的开关状态和指示灯
        for name, enabled in tasks.items():
            row = self._server_rows.get(name)
            if enabled and self.cfg.mcp_enabled.value:
                # 跳过防重复检查，强制重新连接
                server_data = next((s for s in servers if s.get("name") == name), {})
                self._hot_connect(name, server_data, force=True)
            else:
                self._hot_disconnect(name)
            # 直接更新行的开关状态和指示灯：开→黄色(启动中)，关→黑色(已关闭)
            if row:
                row.set_enabled(enabled)
                row.set_status(MCPState.CONNECTING if enabled and self.cfg.mcp_enabled.value else MCPState.DISABLED)

        # 更新 token 估算
        self._update_mcp_token_count()
        # 注意：开关操作不触发全量刷新——列表内容未变，行级开关/指示灯已就地更新；
        # 写盘后的跨窗口同步由 .mcp.json 热重载广播负责（_suppress_hot_reload 抑制自触发）。
        # 此处若 emit serversChanged 会经 GlobalCardController 再触发一次 _refresh() 全量重建，
        # 导致开关 MCP 时整卡闪烁。

    # ── 公开刷新方法（供 settings 弹窗 show 时调用） ──

    def refresh_connections(self):
        """重新连接所有已启用但未连接的服务器"""
        mgr = self._get_mcp_manager()
        servers = self._get_servers()
        if not self.cfg.mcp_enabled.value:
            return
        for s in servers:
            if not s.get("enabled", True):
                continue
            name = s.get("name", "")
            # 只重新连接已断开或未连接过的
            status_list = mgr.get_status()
            already = any(st["name"] == name and st["connected"] for st in status_list)
            if not already:
                self._hot_connect(name, s)

    # ── Token 占用估算 ──────────────────────────────

    def _update_mcp_token_count(self):
        """更新头部 subtitle：服务器计数 + token 占用估算（后台线程计算）

        全部重活（.mcp.json 读取解析 + json.dumps(全部工具 schema) + token 估算）
        都在 daemon 线程执行，结果经 _tokenCountReady 信号（Qt 队列连接）回
        主线程 setCount。主线程路径零 I/O 零重计算——采样器曾抓到同步版本
        在主线程阻塞 2582ms（磁盘 I/O 高压时单次 stat 可达 2s）。
        """
        if getattr(self, "_token_calc_running", False):
            return
        self._token_calc_running = True

        import threading

        def _work():
            try:
                from app.core.token_estimator import estimate_tokens

                servers = self._get_servers()
                count = len(servers)
                enabled_count = sum(1 for s in servers if s.get("enabled", True))
                base = f"{enabled_count}/{count}"

                # 获取已连接的 MCP 工具 schema 并计算 token 数
                mgr = self._get_mcp_manager()
                token_count = 0
                if mgr and self.cfg.mcp_enabled.value:
                    schemas = mgr.get_tool_schemas()
                    if schemas:
                        text = json.dumps(schemas, ensure_ascii=False)
                        token_count = estimate_tokens(text)

                self._tokenCountReady.emit(f"{base} · ~{token_count:,} tokens")
            except RuntimeError:
                pass  # 卡片已销毁
            except Exception as e:
                logger.debug(f"[MCP] token 估算后台计算失败: {e}")
            finally:
                self._token_calc_running = False

        threading.Thread(target=_work, name="mcp-token-calc", daemon=True).start()

    # ── 供外部调用的添加/更新方法 ──────────────────────

    def add_server(self, server_data: dict):
        """添加 MCP 服务器（保留兼容，实际由 PluginManager 管理）"""
        from app.core.plugin_manager import PluginManager
        from app.widgets.tab_manager_window import TabManagerWindow

        pm = PluginManager.get_instance()
        name = server_data.get("name", "")
        servers = self._get_servers()
        if any(s.get("name") == name for s in servers):
            InfoBar.warning(
                title="名称重复",
                content=f"MCP Server '{name}' 已存在",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=TabManagerWindow.get_instance() or self.window(),
            )
            return False
        pm.add_mcp_server(name, server_data)
        self._refresh()
        # 热连接
        if server_data.get("enabled", True) and self.cfg.mcp_enabled.value:
            self._hot_connect(name, server_data)
        return True

    def update_server(self, name: str, server_data: dict):
        """更新 MCP 服务器配置（实际由 PluginManager 管理）"""
        from app.core.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        pm.update_mcp_server(name, server_data)
        self._refresh()
        # 先断开旧连接，再重新连接
        self._hot_disconnect(name)
        if server_data.get("enabled", True) and self.cfg.mcp_enabled.value:
            self._hot_connect(name, server_data)
