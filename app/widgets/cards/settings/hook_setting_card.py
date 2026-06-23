# -*- coding: utf-8 -*-
"""Hook 管理设置卡片"""

import json
from pathlib import Path
from uuid import uuid4

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget
from qfluentwidgets import (
    ExpandSettingCard,
    FluentIcon,
    PushButton,
    SwitchButton,
    ToolButton,
)

from app.utils.design_tokens import ButtonStyles, Sizes, SwitchStyles, scale_font_size
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
        type_colors = {"command": "#4CAF50", "http": "#FF9800", "python": "#2196F3"}
        type_color = type_colors.get(hook_type, "#888")
        type_labels = {"command": "CMD", "http": "HTTP", "python": "PY"}
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
        self.hBoxLayout.addSpacing(12)
        self.hBoxLayout.addWidget(self.switch, 0)
        self.hBoxLayout.addWidget(self.editBtn, 0)
        self.hBoxLayout.addWidget(self.delBtn, 0)
        self.hBoxLayout.setAlignment(Qt.AlignVCenter)

        self.switch.checkedChanged.connect(lambda checked: self.toggled.emit(self.hook_id, checked))

    def _get_effective_command(self) -> str:
        """根据 type 取正确字段"""
        t = self._hook_data.get("type", "command")
        if t == "python":
            raw = self._hook_data.get("function", "") or self._hook_data.get("command", "") or ""
        elif t == "http":
            raw = self._hook_data.get("url", "") or self._hook_data.get("command", "") or ""
        else:
            raw = self._hook_data.get("command", "") or ""
        return raw


class HookEditCard(QWidget):
    """
    Hook 编辑卡片（卡片形态）
    类似 MCPEditCard，放在 BaseSettingsCard 中使用
    """

    saved = pyqtSignal(dict)
    closed = pyqtSignal()

    def __init__(self, hook_data: dict = None, parent=None):
        super().__init__(parent=parent)
        self._hook_data = hook_data or {}
        self._is_new = hook_data is None
        self._setup_ui()
        if not self._is_new:
            self._load_data()

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
        row, _ = _make_row("事件:", self.eventCombo)
        main_layout.addLayout(row)

        # ── 类型 ──
        self.typeCombo = NoWheelComboBox()
        self.typeCombo.addItems(["command", "http", "python"])
        self.typeCombo.currentTextChanged.connect(self._on_type_changed)
        row, _ = _make_row("类型:", self.typeCombo)
        main_layout.addLayout(row)

        # ── 命令 ──
        self.commandEdit = QLineEdit()
        self.commandEdit.setPlaceholderText('如: echo "Hello" 或 python script.py')
        self._cmd_row, self._cmd_label = _make_row("命令:", self.commandEdit)
        main_layout.addLayout(self._cmd_row)

        # ── Matcher（可选） ──
        self.matcherEdit = QLineEdit()
        self.matcherEdit.setPlaceholderText("如: tool:bash 或 .*帮助.*")
        row, _ = _make_row("Matcher:", self.matcherEdit)
        main_layout.addLayout(row)

        # 初始类型
        self._on_type_changed(self.typeCombo.currentText())

    def _on_type_changed(self, hook_type: str):
        """根据类型切换标签文本"""
        if hook_type == "http":
            self._cmd_label.setText("URL:")
            self.commandEdit.setPlaceholderText("如: https://example.com/hook")
        elif hook_type == "python":
            self._cmd_label.setText("脚本:")
            self.commandEdit.setPlaceholderText("如: my_module.hook_handler")
        else:
            self._cmd_label.setText("命令:")
            self.commandEdit.setPlaceholderText('如: echo "Hello" 或 python script.py')

    def _load_data(self):
        d = self._hook_data
        hook_type = d.get("type", "command")
        self.typeCombo.setCurrentText(hook_type)
        self.eventCombo.setCurrentText(d.get("event", "PreToolUse"))
        # 根据类型选择正确的字段加载
        if hook_type == "python":
            self.commandEdit.setText(d.get("function", "") or d.get("command", "") or "")
        elif hook_type == "http":
            self.commandEdit.setText(d.get("url", "") or d.get("command", "") or "")
        else:
            self.commandEdit.setText(d.get("command", "") or "")
        self.matcherEdit.setText(d.get("matcher", ""))

    def get_values(self) -> dict:
        hook_type = self.typeCombo.currentText()
        value = self.commandEdit.text().strip()
        result = {
            "event": self.eventCombo.currentText(),
            "type": hook_type,
            "command": value,
            "matcher": self.matcherEdit.text().strip(),
            "enabled": True
        }
        # 清理旧专用字段，避免类型切换时残留
        result.pop("function", None)
        result.pop("url", None)
        # 根据类型存入正确字段
        if hook_type == "python":
            result["function"] = value
        elif hook_type == "http":
            result["url"] = value
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

    def _add_hook(self, event: str, command: str, matcher: str = "", hook_type: str = "command", enabled: bool = True):
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
        if hook_type == "python":
            hook_entry["function"] = command
        elif hook_type == "http":
            hook_entry["url"] = command

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
