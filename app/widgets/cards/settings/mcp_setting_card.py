# -*- coding: utf-8 -*-
"""
MCP Server 配置卡片

列表卡片 + 编辑卡片，参考 ProviderListSettingCard / ProviderEditCard 模式：
- MCPListSettingCard: 展示服务器列表，含添加/编辑/删除/启停
- MCPEditCard: 编辑/添加服务器的表单卡片（承载在 BaseSettingsCard 中）
"""

import json

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
    QPlainTextEdit,
    QStackedWidget,
)
from loguru import logger
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    ExpandSettingCard,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PushButton,
    StrongBodyLabel,
    SwitchButton,
    ToolButton,
)

from app.utils.config import Settings
from app.utils.design_tokens import Colors, Sizes, ButtonStyles, SwitchStyles, scale_font_size
from app.utils.utils import get_icon, get_font_family_css
from app.widgets.searchable_editable_combobox import SearchableEditableComboBox


# ═══════════════════════════════════════════════════════════
# 共用表单样式
# ═══════════════════════════════════════════════════════════

EDIT_CARD_STYLE = f"""
QWidget {{
    background: transparent;
}}
QLineEdit {{
    background-color: rgba(61, 61, 61, 180);
    color: #ffffff;
    border: 1px solid rgba(85, 85, 85, 150);
    border-radius: 4px;
    padding: 4px 8px;
    {get_font_family_css()}
    font-size: 12px;
}}
QLineEdit:focus {{
    border-color: rgba(0, 120, 212, 200);
}}
QLineEdit::placeholder {{
    color: rgba(255, 255, 255, 0.35);
}}
QPlainTextEdit {{
    background-color: rgba(61, 61, 61, 180);
    color: #ffffff;
    border: 1px solid rgba(85, 85, 85, 150);
    border-radius: 4px;
    padding: 4px 8px;
    {get_font_family_css()}
    font-size: 12px;
}}
QPlainTextEdit:focus {{
    border-color: rgba(0, 120, 212, 200);
}}
"""


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


class _ElidedLabel(QLabel):
    """自动根据可用宽度省略文本的 QLabel"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full_text = text

    def setText(self, text: str):
        self._full_text = text
        self._update_elided()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()

    def _update_elided(self):
        fm = self.fontMetrics()
        elided = fm.elidedText(self._full_text, Qt.ElideRight, self.width())
        super().setText(elided)


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
        self._json_mode = False
        self._init_ui()

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
        result = {
            "mcpServers": {
                name: data
            }
        }
        return json.dumps(result, indent=2, ensure_ascii=False)

    # 模式切换信号（通知外层更新头部按钮）
    modeChanged = pyqtSignal(bool)  # True=JSON模式, False=表单模式

    def _init_ui(self):
        self.setStyleSheet(EDIT_CARD_STYLE)

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

        # ── 名称 ──
        self.nameEdit = QLineEdit()
        self.nameEdit.setPlaceholderText("例如: github, filesystem, my-api")
        if self._is_edit:
            self.nameEdit.setText(self._server_data.get("name", ""))
            self.nameEdit.setReadOnly(True)
        row, _ = _make_row("名称:", self.nameEdit)
        form_layout.addLayout(row)

        # ── 类型 ──
        self.typeCombo = SearchableEditableComboBox()
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
        self.headersEdit.setMaximumHeight(60)
        self.headersEdit.setPlaceholderText('可选 JSON，例如: {"Authorization": "Bearer xxx"}')
        saved_headers = self._server_data.get("headers")
        if saved_headers and isinstance(saved_headers, dict):
            self.headersEdit.setPlainText(json.dumps(saved_headers, indent=2, ensure_ascii=False))
        self._headers_row, self._headers_label = _make_row("Headers:", self.headersEdit)
        form_layout.addLayout(self._headers_row)

        # ── 环境变量（stdio） ──
        self.envEdit = QPlainTextEdit()
        self.envEdit.setMaximumHeight(60)
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
        self.jsonEdit.setStyleSheet(EDIT_CARD_STYLE)
        self.jsonEdit.setPlaceholderText(
            '粘贴标准 MCP 配置（支持两种格式）:\n\n'
            '【格式一】Claude Desktop / Cursor 标准格式:\n'
            '{\n'
            '  "mcpServers": {\n'
            '    "brave-search": {\n'
            '      "command": "npx",\n'
            '      "args": ["-y", "@brave/brave-search-mcp-server"],\n'
            '      "env": {"BRAVE_API_KEY": "xxx"}\n'
            '    }\n'
            '  }\n'
            '}\n\n'
            '【格式二】简化单服务器格式:\n'
            '{\n'
            '  "name": "my-server",\n'
            '  "command": "npx",\n'
            '  "args": ["-y", "some-package"],\n'
            '  "env": {"KEY": "value"}\n'
            '}\n\n'
            '💡 提示: args 含 "--transport http/sse" 时，系统会自动设置连接类型为 http'
        )
        json_data = self._build_json_from_data() if self._server_data else ""
        if json_data:
            self.jsonEdit.setPlainText(json_data)
        json_layout.addWidget(self.jsonEdit)
        # JSON 页加入 stack 索引 1
        self._stack.addWidget(self._json_page)

        # 初始显隐（表单模式按类型显示字段）
        self._stack.setCurrentIndex(0)  # 默认表单模式
        self._on_type_changed(self.typeCombo.currentText())

    def _toggle_mode(self):
        """切换表单/JSON 编辑模式"""
        self._json_mode = not self._json_mode
        if self._json_mode:
            # 切到 JSON 模式：同步表单数据到 JSON 编辑器（标准格式）
            form_data = self._collect_form_data()
            if form_data:
                preview = self._build_json_preview()
                self.jsonEdit.setPlainText(preview if preview else json.dumps(form_data, indent=2, ensure_ascii=False))
            self._stack.setCurrentIndex(1)
        else:
            # 切回表单模式：从 JSON 解析回表单（支持两种格式）
            json_text = self.jsonEdit.toPlainText().strip()
            if json_text:
                try:
                    parsed = json.loads(json_text)
                    parsed = self._parse_mcp_json(parsed)
                    self._apply_json_to_form(parsed)
                except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
                    InfoBar.warning("提示", f"JSON 解析失败: {e}，无法切换回表单模式",
                                    parent=self.window(), duration=3000,
                                    position=InfoBarPosition.BOTTOM)
                    self._json_mode = True  # 保持 JSON 模式
                    self._stack.setCurrentIndex(1)
                    return
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

        # 自动检测类型
        if "type" not in data:
            args = data.get("args", [])
            has_http_transport = any(
                isinstance(a, str) and "--transport" in a and
                i + 1 < len(args) and args[i + 1] in ("http", "sse")
                for i, a in enumerate(args)
            )
            if has_http_transport:
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

    def _on_save(self):
        if self._json_mode:
            # JSON 模式：支持标准 mcpServers 格式和简化格式
            json_text = self.jsonEdit.toPlainText().strip()
            if not json_text:
                InfoBar.warning("提示", "请输入 JSON 配置", parent=self.window(),
                                duration=2000, position=InfoBarPosition.BOTTOM)
                return
            try:
                parsed = json.loads(json_text)
            except json.JSONDecodeError as e:
                InfoBar.warning("提示", f"JSON 格式错误: {e}", parent=self.window(),
                                duration=3000, position=InfoBarPosition.BOTTOM)
                return
            try:
                server_data = self._parse_mcp_json(parsed)
            except (ValueError, KeyError, TypeError) as e:
                InfoBar.warning("提示", f"配置解析失败: {e}", parent=self.window(),
                                duration=3000, position=InfoBarPosition.BOTTOM)
                return
            self.saved.emit(server_data)
            return

        # 表单模式
        name = self.nameEdit.text().strip()
        if not name:
            InfoBar.warning("提示", "请输入服务器名称", parent=self.window(),
                            duration=2000, position=InfoBarPosition.BOTTOM)
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
                InfoBar.warning("提示", "请输入 Command", parent=self.window(),
                                duration=2000, position=InfoBarPosition.BOTTOM)
                return
            server_data["command"] = cmd
            args_text = self.argsEdit.text().strip()
            server_data["args"] = args_text.split() if args_text else []

            env_text = self.envEdit.toPlainText().strip()
            if env_text:
                try:
                    server_data["env"] = json.loads(env_text)
                except json.JSONDecodeError as e:
                    InfoBar.warning("提示", f"环境变量 JSON 格式错误: {e}", parent=self.window(),
                                    duration=3000, position=InfoBarPosition.BOTTOM)
                    return
        else:
            url = self.urlEdit.text().strip()
            if not url:
                InfoBar.warning("提示", "请输入 URL", parent=self.window(),
                                duration=2000, position=InfoBarPosition.BOTTOM)
                return
            server_data["url"] = url
            headers_text = self.headersEdit.toPlainText().strip()
            if headers_text:
                try:
                    server_data["headers"] = json.loads(headers_text)
                except json.JSONDecodeError as e:
                    InfoBar.warning("提示", f"Headers JSON 格式错误: {e}", parent=self.window(),
                                    duration=3000, position=InfoBarPosition.BOTTOM)
                    return

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

    def _setup_ui(self, data: dict):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)

        server_type = data.get("type", "stdio")
        # type_colors = {"stdio": "#2196F3", "sse": "#FF9800", "http": "#4CAF50"}
        # color = type_colors.get(server_type, "#999")
        #
        # type_label = QLabel(server_type.upper())
        # type_label.setStyleSheet(
        #     f"background-color: {color}22; color: {color}; "
        #     f"{get_font_family_css()} font-size: {scale_font_size(11)}px; padding: 2px 8px; border-radius: 4px; font-weight: bold;"
        # )
        # type_label.setFixedWidth(55)
        # type_label.setAlignment(Qt.AlignCenter)
        # layout.addWidget(type_label)

        name_label = StrongBodyLabel(data.get("name", ""))
        name_label.setFixedWidth(100)
        layout.addWidget(name_label)

        if server_type == "stdio":
            desc = f"{data.get('command', '')} {' '.join(data.get('args', []))}".strip()
        else:
            desc = data.get("url", "")
        desc_label = _ElidedLabel(desc)
        desc_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} font-size: {scale_font_size(12)}px;")
        desc_label.setMinimumWidth(40)
        layout.addWidget(desc_label, 1)

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

    def __init__(self, icon, title: str, content: str = None, parent=None):
        self.cfg = Settings.get_instance()
        super().__init__(icon, title, content, parent)
        self._setup_ui()
        self._refresh()

        # 连接信号（主线程处理 UI）
        self._hotConnectResult.connect(self._on_hot_connect_result)

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
        if not hasattr(card, 'hBoxLayout'):
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

    def _hot_connect(self, name: str, config: dict):
        """后台连接单个服务器（不阻塞 UI）"""
        mgr = self._get_mcp_manager()
        if not self.cfg.mcp_enabled.value:
            return

        # 防重复：已连接的不再触发
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
        if success:
            logger.info(f"[MCP] '{name}' 热连接成功")
        else:
            # 提取友好提示
            hint = error_msg or "未知错误"
            if "请检查配置类型是否正确" in hint:
                # 拆分为标题和内容
                parts = hint.split("（", 1)
                display_msg = parts[0]
                detail = "（" + parts[1] if len(parts) > 1 else ""
                logger.warning(f"[MCP] '{name}' 热连接失败: {display_msg}")
                InfoBar.error(
                    title=f"MCP 连接失败: {name}",
                    content=f"{display_msg}\n{detail}" if detail else display_msg,
                    parent=self.window(),
                    duration=8000,
                    position=InfoBarPosition.BOTTOM,
                )
            elif hint != "未知错误":
                logger.warning(f"[MCP] '{name}' 热连接失败: {hint}")
                InfoBar.error(
                    title=f"MCP 连接失败: {name}",
                    content=hint,
                    parent=self.window(),
                    duration=6000,
                    position=InfoBarPosition.BOTTOM,
                )
            else:
                logger.warning(f"[MCP] '{name}' 热连接失败")
                InfoBar.error(
                    title=f"MCP 连接失败",
                    content=f"'{name}' 连接失败，请检查配置是否正确",
                    parent=self.window(),
                    duration=5000,
                    position=InfoBarPosition.BOTTOM,
                )
        self.serversChanged.emit()

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
        self.cfg.set(self.cfg.mcp_enabled, enabled, save=True)
        if enabled:
            servers = self.cfg.mcp_servers.value or []
            for s in servers:
                if s.get("enabled", True):
                    self._hot_connect(s.get("name", ""), s)
        else:
            self._hot_disconnect_all()
        self.serversChanged.emit()

    # ── 列表刷新 ──────────────────────────────────────

    def _refresh(self):
        """刷新服务器列表（保留展开状态）"""
        was_expanded = self.isExpand

        # 稳妥方式清空 viewLayout：takeAt + 删除 widget
        while self.viewLayout.count():
            item = self.viewLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        servers = self.cfg.mcp_servers.value
        if not servers:
            empty_label = QLabel("暂无 MCP 服务器，点击「添加服务器」创建", self.view)
            empty_label.setStyleSheet(f"color: #888; padding: 16px; {get_font_family_css()} font-size: {scale_font_size(12)}px;")
            empty_label.setAlignment(Qt.AlignCenter)
            self.viewLayout.addWidget(empty_label)
        else:
            for server_data in servers:
                row = MCPServerRow(server_data, self.view)
                row.removeRequested.connect(self._on_remove_server)
                row.editRequested.connect(self._show_edit_dialog)
                row.enabledChanged.connect(self._on_enabled_changed)
                self.viewLayout.addWidget(row)

            count = len(servers)
            enabled_count = sum(1 for s in servers if s.get("enabled", True))
            self.setCount(f"{enabled_count}/{count}")

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

    def setCount(self, text: str):
        card = self.card
        if hasattr(card, 'contentLabel'):
            card.contentLabel.setText(text)

    def _save_servers(self, servers: list):
        self.cfg.set(self.cfg.mcp_servers, servers, save=True)
        self._refresh()
        self.serversChanged.emit()

    def _on_remove_server(self, name: str):
        from qfluentwidgets import Dialog
        w = Dialog("确定要删除这个 MCP 服务器吗?", f'删除 "{name}" 后将不再出现在列表中。', self.window())
        w.yesSignal.connect(lambda: self._do_remove(name))
        w.exec_()

    def _do_remove(self, name: str):
        # 热断开
        self._hot_disconnect(name)
        servers = list(self.cfg.mcp_servers.value or [])
        servers = [s for s in servers if s.get("name") != name]
        self._save_servers(servers)

    def _show_edit_dialog(self, name: str):
        servers = list(self.cfg.mcp_servers.value or [])
        server_data = next((s for s in servers if s.get("name") == name), None)
        if server_data:
            self.showEditCard.emit(name, server_data)

    def _on_enabled_changed(self, name: str, enabled: bool):
        servers = list(self.cfg.mcp_servers.value or [])
        for s in servers:
            if s.get("name") == name:
                s["enabled"] = enabled
                break
        self.cfg.set(self.cfg.mcp_servers, servers, save=True)

        # 热连接/断开
        if enabled and self.cfg.mcp_enabled.value:
            server_data = next((s for s in servers if s.get("name") == name), {})
            self._hot_connect(name, server_data)
        else:
            self._hot_disconnect(name)

        self.serversChanged.emit()

    # ── 公开刷新方法（供 settings 弹窗 show 时调用） ──

    def refresh_connections(self):
        """重新连接所有已启用但未连接的服务器（修复新配置不生效问题）"""
        mgr = self._get_mcp_manager()
        servers = self.cfg.mcp_servers.value or []
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

    # ── 供外部调用的添加/更新方法 ──────────────────────

    def add_server(self, server_data: dict):
        servers = list(self.cfg.mcp_servers.value or [])
        name = server_data.get("name", "")
        if any(s.get("name") == name for s in servers):
            InfoBar.warning(title="名称重复", content=f"MCP Server '{name}' 已存在",
                            position=InfoBarPosition.BOTTOM, duration=3000, parent=self.window())
            return False
        servers.append(server_data)
        self._save_servers(servers)
        # 热连接
        if server_data.get("enabled", True) and self.cfg.mcp_enabled.value:
            self._hot_connect(name, server_data)
        return True

    def update_server(self, name: str, server_data: dict):
        servers = list(self.cfg.mcp_servers.value or [])
        for i, s in enumerate(servers):
            if s.get("name") == name:
                servers[i] = server_data
                break
        self._save_servers(servers)
        # 先断开旧连接，再重新连接
        self._hot_disconnect(name)
        if server_data.get("enabled", True) and self.cfg.mcp_enabled.value:
            self._hot_connect(name, server_data)
