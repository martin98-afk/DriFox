# -*- coding: utf-8 -*-
"""Hook 管理设置卡片"""

import json
from pathlib import Path
from uuid import uuid4

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    ExpandSettingCard,
    FluentIcon,
    PushButton,
    SwitchButton,
    ToolButton,
)

from app.tools.tool_name_mapper import ToolNameMapper
from app.utils.design_tokens import ButtonStyles, Colors, Sizes, SwitchStyles, scale_font_size
from app.utils.utils import get_app_data_dir, get_font_family_css
from app.widgets.cards.settings.mcp_setting_card import EDIT_CARD_STYLE, NoWheelComboBox, _make_row
from app.widgets.elided_label import _ElidedLabel

# 事件顺序定义（按触发先后排列）
# - UserPromptSubmit：用户提交原始 prompt，发生在 PreUserMessage 之前
# - Stop：流式输出被 stop_streaming 停止时，同步触发
HOOK_EVENT_ORDER = [
    "SessionStart",
    "UserPromptSubmit",
    "PreUserMessage", "PostUserMessage",
    "PreAssistantMessage", "PostAssistantMessage",
    "PreToolUse", "PostToolUse",
    "Stop",
]


class HookItem(QWidget):
    """单个 Hook 条目"""
    removed = pyqtSignal(str)  # hook_id
    edited = pyqtSignal(str)   # hook_id
    toggled = pyqtSignal(str, bool)  # hook_id, enabled

    def __init__(self, hook_data: dict, parent=None):
        super().__init__(parent=parent)
        self.hook_id = hook_data.get("id", "")
        self._hook_data = hook_data
        self._setup_ui()

    def _setup_ui(self):
        self.setStyleSheet("background-color: transparent;")
        self.hBoxLayout = QHBoxLayout(self)

        # 来源标签（彩色小 tag）
        source_type = self._hook_data.get("_source_type", "user")
        display_name = self._hook_data.get("_display_name", "自定义")
        source_color = {"plugin": "#e74c3c", "skill": "#3498db", "user": "#2ecc71"}.get(source_type, "#888")
        if source_type == "user":
            source_text = "自定义"
        else:
            source_text = display_name[:12] + ("…" if len(display_name) > 12 else "")
        self.sourceLabel = QLabel(source_text, self)
        self.sourceLabel.setStyleSheet(
            f"background-color: {source_color}; color: white; "
            f"{get_font_family_css()} font-size: {scale_font_size(10)}px; "
            f"padding: 1px 6px; border-radius: 4px; font-weight: bold;"
        )
        self.sourceLabel.setFixedHeight(18)

        # 类型标签
        hook_type = self._hook_data.get("type", "command")
        type_colors = {"command": "#4CAF50", "http": "#FF9800", "python": "#2196F3", "prompt": "#9C27B0"}
        type_color = type_colors.get(hook_type, "#888")
        type_labels = {"command": "CMD", "http": "HTTP", "python": "PY", "prompt": "PROMPT"}
        self.typeLabel = QLabel(type_labels.get(hook_type, hook_type.upper()), self)
        self.typeLabel.setStyleSheet(
            f"background-color: {type_color}; color: white; "
            f"{get_font_family_css()} font-size: {scale_font_size(10)}px; "
            f"padding: 1px 6px; border-radius: 4px; font-weight: bold;"
        )
        self.typeLabel.setFixedHeight(18)

        # 命令文本
        display_cmd = self._get_effective_command()
        self.commandLabel = _ElidedLabel(display_cmd, self)
        self.commandLabel.setObjectName("titleLabel")
        self.commandLabel.setStyleSheet(
            f"{get_font_family_css()} font-size: {scale_font_size(13)}px;"
        )
        self.commandLabel.setMinimumWidth(40)

        # Windows 标签（仅在 commandWindows 存在时显示）
        self._winLabel = None
        if self._hook_data.get("commandWindows"):
            self._winLabel = QLabel("Win", self)
            self._winLabel.setStyleSheet(
                f"background-color: #FF8C00; color: white; "
                f"{get_font_family_css()} font-size: {scale_font_size(9)}px; "
                f"padding: 1px 4px; border-radius: 3px; font-weight: bold;"
            )
            self._winLabel.setFixedHeight(16)

        # 开关
        self.switch = SwitchButton(self)
        SwitchStyles.configure(self.switch)
        self.switch.setChecked(self._hook_data.get("enabled", True))

        # 编辑/删除按钮（所有来源都可用）
        self.editBtn = ToolButton(FluentIcon.EDIT)
        self.editBtn.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        self.editBtn.setStyleSheet(ButtonStyles.tool_button())
        self.editBtn.clicked.connect(lambda: self.edited.emit(self.hook_id))

        self.delBtn = ToolButton(FluentIcon.CLOSE)
        self.delBtn.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        self.delBtn.setStyleSheet(ButtonStyles.tool_button())
        self.delBtn.clicked.connect(lambda: self.removed.emit(self.hook_id))

        self.setFixedHeight(40)
        self.hBoxLayout.setContentsMargins(8, 0, 16, 0)  # ponytail: 左 padding 从 48 缩到 8
        self.hBoxLayout.addWidget(self.sourceLabel, 0)
        self.hBoxLayout.addSpacing(6)
        self.hBoxLayout.addWidget(self.typeLabel, 0)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.commandLabel, 1)
        if self._winLabel:
            self.hBoxLayout.addSpacing(4)
            self.hBoxLayout.addWidget(self._winLabel, 0)
        self.hBoxLayout.addSpacing(12)
        self.hBoxLayout.addWidget(self.switch, 0)
        self.hBoxLayout.addWidget(self.editBtn, 0)
        self.hBoxLayout.addWidget(self.delBtn, 0)
        self.hBoxLayout.setAlignment(Qt.AlignVCenter)

        self.switch.checkedChanged.connect(lambda checked: self.toggled.emit(self.hook_id, checked))

    def _get_effective_command(self) -> str:
        """根据 type 取正确字段用于预览"""
        t = self._hook_data.get("type", "command")
        if t == "python":
            raw = self._hook_data.get("function", "") or self._hook_data.get("command", "") or ""
        elif t == "http":
            raw = self._hook_data.get("url", "") or self._hook_data.get("command", "") or ""
        elif t == "prompt":
            raw = self._hook_data.get("prompt", "") or self._hook_data.get("command", "") or ""
        else:
            raw = self._hook_data.get("command", "") or ""
        return raw


class HookEditCard(QWidget):
    """
    Hook 编辑卡片（卡片形态）
    类似 MCPEditCard，放在 BaseSettingsCard 中使用

    增强:
    - commandWindows 字段（编辑已有 command 插件时显示）
    - statusMessage 字段
    - 智能 Matcher 选择：
      - SessionStart → startup/resume/clear/compact 勾选框
      - PreToolUse/PostToolUse → 工具名列表勾选框
      - 其他事件 → 仅文本输入框
    - 勾选框与文本输入框双向同步
    - 来源自动填充（从已加载插件解析）
    """

    # SessionStart 会话状态选项
    SESSION_STATES = ["startup", "resume", "clear", "compact"]

    saved = pyqtSignal(dict)
    closed = pyqtSignal()

    def __init__(self, hook_data: dict = None, parent=None, hook_manager=None):
        super().__init__(parent=parent)
        self._hook_data = hook_data or {}
        self._is_new = hook_data is None
        self._hook_manager = hook_manager
        self._matcher_checkboxes = []  # [(QCheckBox, value), ...]
        self._syncing_matcher = False  # 防递归同步标志
        self._setup_ui()
        if not self._is_new:
            self._load_data()
        # 初始化后同步 matcher 状态
        self._sync_matcher_text_from_checks()

    def get_original_data(self) -> dict:
        """返回原始 hook 数据（编辑时使用），新增时返回空 dict"""
        return dict(self._hook_data) if not self._is_new else {}

    def _setup_ui(self):
        self.setStyleSheet(EDIT_CARD_STYLE)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 2, 4, 2)
        main_layout.setSpacing(6)

        # ── 事件 ──
        self.eventCombo = NoWheelComboBox()
        self.eventCombo.addItems(HOOK_EVENT_ORDER)
        self.eventCombo.currentTextChanged.connect(self._on_event_changed)
        row, _ = _make_row("事件:", self.eventCombo)
        main_layout.addLayout(row)

        # ── 类型 ──
        self.typeCombo = NoWheelComboBox()
        self.typeCombo.addItems(["command", "http", "python", "prompt"])
        self.typeCombo.currentTextChanged.connect(self._on_type_changed)
        row, _ = _make_row("类型:", self.typeCombo)
        main_layout.addLayout(row)

        # ── 命令 ──
        self.commandEdit = QPlainTextEdit()
        self.commandEdit.setMaximumHeight(100)
        self.commandEdit.setMinimumHeight(48)
        self.commandEdit.setPlaceholderText('如: echo "Hello" 或 python script.py')
        self._cmd_row, self._cmd_label = _make_row("命令:", self.commandEdit)
        main_layout.addLayout(self._cmd_row)

        # ── Windows 命令（仅编辑已有 commandWindows 的 hook 时显示） ──
        self.commandWindowsEdit = QPlainTextEdit()
        self.commandWindowsEdit.setMaximumHeight(80)
        self.commandWindowsEdit.setMinimumHeight(36)
        self.commandWindowsEdit.setPlaceholderText("Windows 专用命令（可选）")
        # 用 QFrame 承载整行以便整体 setVisible（layout 本身没有 setVisible）
        self._cmdwin_row = QFrame()
        _cmdwin_inner, self._cmdwin_label = _make_row("Win 命令:", self.commandWindowsEdit)
        self._cmdwin_row.setLayout(_cmdwin_inner)
        self._cmdwin_row.setVisible(False)
        main_layout.addWidget(self._cmdwin_row)

        # ── 状态消息 ──
        self.statusMessageEdit = QLineEdit()
        self.statusMessageEdit.setPlaceholderText("如: Loading ponytail mode...（可选）")
        row, _ = _make_row("状态消息:", self.statusMessageEdit)
        main_layout.addLayout(row)

        # ── 执行结果插入消息列表 ──
        self.addOutputCtxSwitch = SwitchButton()
        SwitchStyles.configure(self.addOutputCtxSwitch)
        self.addOutputCtxSwitch.setChecked(True)
        # 用 QFrame 承载整行以便整体 setVisible
        self._add_output_row = QFrame()
        _add_output_inner = QHBoxLayout()
        _add_output_inner.setSpacing(8)
        _add_output_label = BodyLabel("输出到消息:")
        _add_output_label.setFixedWidth(70)
        _add_output_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        _add_output_inner.addWidget(_add_output_label)
        _add_output_inner.addWidget(self.addOutputCtxSwitch, 0)
        _add_output_hint = QLabel("执行结果将插入对话消息列表")
        _add_output_hint.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        _add_output_inner.addWidget(_add_output_hint, 1)
        self._add_output_row.setLayout(_add_output_inner)
        main_layout.addWidget(self._add_output_row)

        # ── Matcher 智能选择区 ──
        matcher_section = QVBoxLayout()
        matcher_section.setSpacing(4)

        # 勾选框容器（使用 QVBoxLayout，内嵌多行）
        self._matcher_checks_frame = QFrame()
        self._matcher_checks_frame.setVisible(False)
        # 主 layout 为垂直，内嵌行 layout
        self._matcher_checks_layout = QVBoxLayout(self._matcher_checks_frame)
        self._matcher_checks_layout.setContentsMargins(70, 0, 0, 4)  # 缩进对齐文本输入框
        self._matcher_checks_layout.setSpacing(4)
        matcher_section.addWidget(self._matcher_checks_frame)

        # 文本输入框
        self.matcherEdit = QLineEdit()
        self.matcherEdit.setPlaceholderText("如: startup|clear 或 Edit|Write|MultiEdit 或 .*帮助.*")
        self.matcherEdit.textChanged.connect(self._on_matcher_text_changed)
        row, _ = _make_row("Matcher:", self.matcherEdit)
        matcher_section.addLayout(row)

        main_layout.addLayout(matcher_section)

        # 来源提示
        self._source_label = QLabel("")
        self._source_label.setStyleSheet(
            f"color: #888; {get_font_family_css()} font-size: {scale_font_size(11)}px; padding: 2px 4px;"
        )
        self._source_label.setVisible(False)
        main_layout.addWidget(self._source_label)

        # 初始类型
        self._on_type_changed(self.typeCombo.currentText())

        # 初始事件勾选框（新卡片时 eventCombo 有默认值但信号未触发）
        self._rebuild_matcher_checks(self.eventCombo.currentText())

    def _on_event_changed(self, event_name: str):
        """事件切换时重建 matcher 勾选框"""
        self._rebuild_matcher_checks(event_name)
        self._sync_matcher_text_from_checks()

    def _rebuild_matcher_checks(self, event_name: str):
        """根据事件类型重建 matcher 勾选框"""
        # 清除旧勾选框
        for cb, _ in self._matcher_checkboxes:
            self._matcher_checks_layout.removeWidget(cb)
            cb.deleteLater()
        self._matcher_checkboxes.clear()

        # 根据事件类型获取选项列表
        options = []
        if event_name == "SessionStart":
            options = list(self.SESSION_STATES)
        elif event_name in ("PreToolUse", "PostToolUse"):
            options = sorted(ToolNameMapper.ALIAS_MAP.keys())

        if not options:
            self._matcher_checks_frame.setVisible(False)
            return

        self._matcher_checks_frame.setVisible(True)

        # 主题感知的 checkbox 样式
        Colors.refresh()
        _cb_style = (
            f"QCheckBox {{ color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} font-size: {scale_font_size(12)}px; }} "
            f"QCheckBox::indicator {{ width: 16px; height: 16px; }}"
        )

        # SessionStart 状态少，单行横排
        if event_name == "SessionStart":
            _row_layout = QHBoxLayout()
            _row_layout.setSpacing(8)
            for opt in options:
                cb = QCheckBox(opt)
                cb.setStyleSheet(_cb_style)
                cb.toggled.connect(self._on_matcher_check_toggled)
                _row_layout.addWidget(cb)
                self._matcher_checkboxes.append((cb, opt))
            _row_layout.addStretch()
            self._matcher_checks_layout.addLayout(_row_layout)
        else:
            # 工具名多，每行最多 6 个
            _per_row = 6
            for i in range(0, len(options), _per_row):
                _row_layout = QHBoxLayout()
                _row_layout.setSpacing(6)
                for opt in options[i:i + _per_row]:
                    cb = QCheckBox(opt)
                    cb.setStyleSheet(_cb_style)
                    cb.toggled.connect(self._on_matcher_check_toggled)
                    _row_layout.addWidget(cb)
                    self._matcher_checkboxes.append((cb, opt))
                _row_layout.addStretch()
                self._matcher_checks_layout.addLayout(_row_layout)

    def _on_matcher_check_toggled(self):
        """勾选框变化 -> 更新文本输入框"""
        if self._syncing_matcher:
            return
        self._sync_matcher_text_from_checks()

    def _on_matcher_text_changed(self, text: str):
        """文本输入框变化 -> 更新勾选框"""
        if self._syncing_matcher:
            return
        self._sync_matcher_checks_from_text(text)

    def _sync_matcher_text_from_checks(self):
        """勾选框状态 -> pipe 分隔文本"""
        self._syncing_matcher = True
        try:
            selected = [opt for cb, opt in self._matcher_checkboxes if cb.isChecked()]
            text = "|".join(selected)
            self.matcherEdit.setText(text)
        finally:
            self._syncing_matcher = False

    @staticmethod
    def _normalize_matcher_part(part: str) -> str:
        """归一化 matcher 片段：去 tool: 前缀、大小写不敏感、ToolNameMapper 别名"""
        p = part.strip()
        if not p:
            return ""
        # 去除 tool: 前缀
        if p.startswith("tool:"):
            p = p[5:]
        # 通过 ToolNameMapper 归一化（处理别名）
        native = ToolNameMapper.to_native(p)
        if native != p:
            return native
        # 兜底：小写化
        return p.lower()

    def _sync_matcher_checks_from_text(self, text: str):
        """pipe 分隔文本 -> 勾选框状态（大小写/别名/前缀不敏感）"""
        self._syncing_matcher = True
        try:
            if not text:
                for cb, _ in self._matcher_checkboxes:
                    cb.setChecked(False)
                return
            # 归一化所有目标值
            parts = set()
            for p in text.split("|"):
                normalized = self._normalize_matcher_part(p)
                if normalized:
                    parts.add(normalized)
            for cb, opt in self._matcher_checkboxes:
                cb.setChecked(opt.lower() in parts)
        finally:
            self._syncing_matcher = False

    def _on_type_changed(self, hook_type: str):
        """根据类型切换标签文本和可见字段"""
        is_command = hook_type == "command"
        is_prompt = hook_type == "prompt"

        if hook_type == "http":
            self._cmd_label.setText("URL:")
            self.commandEdit.setPlaceholderText("如: https://example.com/hook")
        elif hook_type == "python":
            self._cmd_label.setText("脚本:")
            self.commandEdit.setPlaceholderText("如: my_module.hook_handler")
        elif hook_type == "prompt":
            self._cmd_label.setText("提示:")
            self.commandEdit.setPlaceholderText("如: Before ending, check for uncommitted changes...")
        else:
            self._cmd_label.setText("命令:")
            self.commandEdit.setPlaceholderText('如: echo "Hello" 或 python script.py')

        # commandWindows 仅对 command 类型且在编辑已有 commandWindows 的 hook 时显示
        has_cmdwin = bool(self._hook_data.get("commandWindows", ""))
        if self._cmdwin_row:
            self._cmdwin_row.setVisible(is_command and has_cmdwin)

        # 状态消息仅对 command 类型有效
        self.statusMessageEdit.setVisible(is_command)

        # 输出到消息切换：prompt 类型固定为 True（隐藏开关），其他类型可配置
        self._add_output_row.setVisible(not is_prompt)

    def _load_data(self):
        d = self._hook_data
        hook_type = d.get("type", "command")
        self.typeCombo.setCurrentText(hook_type)
        self.eventCombo.setCurrentText(d.get("_event", "PreToolUse"))
        # 根据类型选择正确的字段加载
        if hook_type == "python":
            self.commandEdit.setPlainText(d.get("function", "") or d.get("command", "") or "")
        elif hook_type == "http":
            self.commandEdit.setPlainText(d.get("url", "") or d.get("command", "") or "")
        elif hook_type == "prompt":
            self.commandEdit.setPlainText(d.get("prompt", "") or d.get("command", "") or "")
        else:
            self.commandEdit.setPlainText(d.get("command", "") or "")

        # commandWindows（仅当存在时显示）
        if d.get("commandWindows", ""):
            self.commandWindowsEdit.setPlainText(d["commandWindows"])
            if self._cmdwin_row:
                self._cmdwin_row.setVisible(True)

        # statusMessage
        self.statusMessageEdit.setText(d.get("statusMessage", "") or d.get("status_message", "") or "")

        # matcher
        matcher = d.get("matcher", "")
        self.matcherEdit.setText(matcher)

        # 重建勾选框并同步
        event = d.get("_event", self.eventCombo.currentText())
        self._rebuild_matcher_checks(event)
        self._sync_matcher_checks_from_text(matcher)

        # 来源标注
        source_type = d.get("_source_type", "")
        display_name = d.get("_display_name", "")
        if source_type and display_name and source_type != "user":
            self._source_label.setText(f"📦 来源: {display_name} ({source_type})")
            self._source_label.setVisible(True)

        # add_output_to_context
        add_output = d.get("add_output_to_context", True)
        if isinstance(add_output, bool):
            self.addOutputCtxSwitch.setChecked(add_output)
        # prompt 类型固定输出到消息，开关隐藏
        if hook_type == "prompt":
            self.addOutputCtxSwitch.setChecked(True)

        # 重新同步 UI 可见性（确保 _cmdwin_row 等与当前数据一致）
        self._on_type_changed(hook_type)

    def get_values(self) -> dict:
        hook_type = self.typeCombo.currentText()
        value = self.commandEdit.toPlainText().strip()
        matcher = self.matcherEdit.text().strip()

        # prompt 类型始终输出到消息
        add_output = True if hook_type == "prompt" else self.addOutputCtxSwitch.isChecked()

        result = {
            "event": self.eventCombo.currentText(),
            "type": hook_type,
            "command": value,
            "matcher": matcher,
            "enabled": True,
            "add_output_to_context": add_output,
        }

        # commandWindows
        cmdwin = self.commandWindowsEdit.toPlainText().strip()
        if cmdwin:
            result["commandWindows"] = cmdwin

        # statusMessage
        status_msg = self.statusMessageEdit.text().strip()
        if status_msg:
            result["statusMessage"] = status_msg

        # 清理旧专用字段，避免类型切换时残留
        result.pop("function", None)
        result.pop("url", None)
        result.pop("prompt", None)
        # 根据类型存入正确字段
        if hook_type == "python":
            result["function"] = value
        elif hook_type == "http":
            result["url"] = value
        elif hook_type == "prompt":
            result["prompt"] = value
        return result

    def _on_save(self):
        values = self.get_values()
        if not values["event"] or not values["command"]:
            return
        self.saved.emit(values)

    def get_title(self) -> str:
        if self._is_new:
            return "➕ 添加 Hook"
        return "✏️ 编辑 Hook"


class HookListSettingCard(ExpandSettingCard):
    """Hook 管理设置卡片"""

    hooksChanged = pyqtSignal()
    showAddHookCard = pyqtSignal()  # 显示添加 Hook 卡片
    showEditHookCard = pyqtSignal(str, dict)  # 显示编辑 Hook 卡片: (hook_id, hook_data)

    def __init__(self, icon: QIcon, title: str, content: str = None, parent=None, home=None,
                 hook_manager=None):
        self.home = home
        self._hook_manager = hook_manager
        super().__init__(icon, title, content, parent)
        self.title = title
        self.grouped_hooks = {"plugin": {}, "skill": {}, "user": {}}
        self._hooks_config_file = self._get_global_hooks_file()
        self._setup_ui()
        self._refresh()

    def _get_global_hooks_file(self) -> Path:
        """获取全局 hooks 文件路径"""
        try:
            from app.core.plugin_manager import PluginManager
            pm = PluginManager.get_instance()
            if pm.is_initialized():
                return pm.get_global_hooks_file()
        except Exception:
            pass
        return get_app_data_dir() / "plugins" / "user-custom" / "hooks" / "hooks.json"

    def _setup_ui(self):
        self.viewLayout.setSpacing(0)
        self.viewLayout.setAlignment(Qt.AlignTop)
        self.viewLayout.setContentsMargins(8, 0, 8, 0)

        self.addButton = PushButton("添加", self, FluentIcon.ADD)
        self.addButton.setObjectName("_hook_add_btn")
        self.addButton.clicked.connect(self.showAddHookCard.emit)

        self.addWidget(self.addButton)
        self._update_button_position()

    def _update_button_position(self):
        """将 addButton 移到卡片头部 expandButton 左侧"""
        card = self.card
        if not hasattr(card, 'hBoxLayout'):
            return
        card.hBoxLayout.removeWidget(self.addButton)
        for i in range(card.hBoxLayout.count()):
            item = card.hBoxLayout.itemAt(i)
            if item.widget() == card.expandButton:
                card.hBoxLayout.removeItem(card.hBoxLayout.itemAt(i - 1))
                card.hBoxLayout.insertWidget(i - 1, self.addButton, 0, Qt.AlignRight)
                card.hBoxLayout.insertSpacing(i - 1, 4)
                card.hBoxLayout.insertSpacing(i + 1, 4)
                break

    def _refresh(self, reload=True):
        """刷新 hook 列表"""
        was_expanded = self.isExpand
        if reload:
            self._load_hooks()

        # 清空 viewLayout
        while self.viewLayout.count():
            item = self.viewLayout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        self._render_hooks()

        from PyQt5.QtCore import QCoreApplication
        QCoreApplication.processEvents()
        self.viewLayout.activate()
        self.view.updateGeometry()

        self._adjustViewSize()
        if was_expanded:
            h = self.viewLayout.sizeHint().height()
            if h > 0:
                self.setFixedHeight(self.card.height() + h)

    def _load_hooks(self):
        """从 HookManager 加载分组后的 hooks"""
        self.grouped_hooks = {"plugin": {}, "skill": {}, "user": {}}
        if self._hook_manager:
            self.grouped_hooks = self._hook_manager.get_all_hooks_grouped()

    def _render_hooks(self):
        """按事件分组渲染 hooks"""
        # 收集所有有 hook 的事件
        has_any = False
        for source in ("plugin", "skill", "user"):
            source_hooks = self.grouped_hooks.get(source, {})
            for event in HOOK_EVENT_ORDER:
                if source_hooks.get(event):
                    has_any = True
                    break

        if not has_any:
            empty_label = QLabel("暂无 Hooks，点击「+ 添加」创建", self.view)
            empty_label.setStyleSheet(
                f"color: #888; {get_font_family_css()} font-size: {scale_font_size(12)}px; padding: 16px;"
            )
            empty_label.setAlignment(Qt.AlignCenter)
            self.viewLayout.addWidget(empty_label)
            return

        # 按事件顺序渲染
        for event in HOOK_EVENT_ORDER:
            event_hooks = []
            for source in ("plugin", "skill", "user"):
                hooks = self.grouped_hooks.get(source, {}).get(event, [])
                for h in hooks:
                    h = dict(h)  # 深拷贝避免修改原数据
                    event_hooks.append(h)

            if not event_hooks:
                continue

            # 事件标题
            header = QLabel(f"Event: {event}", self.view)
            header.setStyleSheet(
                f"background-color: #F0F0F0; color: #333; font-weight: bold; "
                f"{get_font_family_css()} font-size: {scale_font_size(12)}px; padding: 6px 8px;"
            )
            self.viewLayout.addWidget(header)

            # Hook 条目
            for hook_data in event_hooks:
                hook_id = hook_data.get("id", "")
                item = HookItem(hook_data, self.view)
                item.removed.connect(lambda hid: self._delete_hook_by_id(hid))
                item.edited.connect(lambda hid: self._edit_hook_by_id(hid))
                item.toggled.connect(lambda hid, enabled: self._toggle_hook_by_id(hid, enabled))
                self.viewLayout.addWidget(item)

    def _edit_hook_by_id(self, hook_id: str):
        """在所有分组中查找 hook 数据并发出编辑信号"""
        for source in ("plugin", "skill", "user"):
            for event, hooks in list(self.grouped_hooks.get(source, {}).items()):
                for h in hooks:
                    if h.get("id") == hook_id:
                        hook_with_event = dict(h)
                        hook_with_event["_event"] = event
                        hook_with_event["_source_type"] = source
                        self.showEditHookCard.emit(hook_id, hook_with_event)
                        return

    def _delete_hook_by_id(self, hook_id: str):
        """删除 hook"""
        if self._hook_manager:
            self._hook_manager.delete_hook_by_id(hook_id)
            self._refresh(reload=True)
            self.hooksChanged.emit()

    def _toggle_hook_by_id(self, hook_id: str, enabled: bool):
        """切换 hook 启用状态"""
        if self._hook_manager:
            self._hook_manager.toggle_hook_by_id(hook_id, enabled)
            self._refresh(reload=True)
            self.hooksChanged.emit()

    def _add_hook(self, event: str, command: str, matcher: str = "", hook_type: str = "command",
                  enabled: bool = True, commandWindows: str = "", statusMessage: str = ""):
        """添加新 hook（写入 user-custom hooks 文件）"""
        # 构建 hook 条目
        hook_id = uuid4().hex
        hook_entry = {
            "id": hook_id,
            "type": hook_type,
            "command": command,
            "matcher": matcher or "",
            "enabled": enabled
        }
        if commandWindows:
            hook_entry["commandWindows"] = commandWindows
        if statusMessage:
            hook_entry["statusMessage"] = statusMessage
        if hook_type == "python":
            hook_entry["function"] = command
        elif hook_type == "http":
            hook_entry["url"] = command
        elif hook_type == "prompt":
            hook_entry["prompt"] = command

        # 加载/创建配置文件
        config_file = self._hooks_config_file
        config_file.parent.mkdir(parents=True, exist_ok=True)

        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except Exception:
                config = {}
        else:
            config = {"hooks": {}}

        raw_hooks = config.get("hooks", config)

        # 追加到对应事件
        if event not in raw_hooks:
            raw_hooks[event] = []

        raw_hooks[event].append({
            "matcher": matcher or "",
            "hooks": [hook_entry]
        })

        # 写文件
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        # 同步 HookManager 内存
        if self._hook_manager:
            self._hook_manager.reload_global_hooks(str(config_file))

        self._refresh(reload=True)  # reload=True: 从 HookManager 重新读取 grouped_hooks
        self.hooksChanged.emit()
