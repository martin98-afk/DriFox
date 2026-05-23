# -*- coding: utf-8 -*-
"""Hook 管理设置卡片"""

import json
from pathlib import Path

from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout
from qfluentwidgets import (
    ExpandSettingCard,
    PushButton,
    FluentIcon,
    SwitchButton,
    ToolButton,
)

from app.utils.utils import get_app_data_dir, get_font_family_css
from app.utils.design_tokens import scale_font_size, Sizes, ButtonStyles, SwitchStyles
from app.widgets.cards.settings.mcp_setting_card import _ElidedLabel, EDIT_CARD_STYLE, _make_row
from app.widgets.searchable_editable_combobox import SearchableEditableComboBox
from app.widgets.cards.settings.system_card_frame import SystemCardFrame


class HookItem(QWidget):
    """单个 Hook 条目"""
    removed = pyqtSignal(int)  # 发送 hook 索引
    edited = pyqtSignal(int)  # 发送 hook 索引
    toggled = pyqtSignal(int, bool)  # 索引, 启用状态
    
    def __init__(self, event_name: str, hook_data: dict, index: int, parent=None):
        super().__init__(parent=parent)
        self.event_name = event_name
        self.hook_data = hook_data
        self.index = index
        self._setup_ui()
    
    def _setup_ui(self):
        self.setStyleSheet("background-color: transparent;")
        self.hBoxLayout = QHBoxLayout(self)
        
        # 命令显示（截断到 50 字符）
        command = self.hook_data.get("command", self.hook_data.get("url", ""))
        display_cmd = command[:50] + ("..." if len(command) > 50 else "")
        self.commandLabel = _ElidedLabel(display_cmd, self)
        self.commandLabel.setObjectName("titleLabel")
        self.commandLabel.setStyleSheet(f"{get_font_family_css()} font-size: {scale_font_size(13)}px;")
        self.commandLabel.setMinimumWidth(40)
        
        # Matcher 标签
        matcher = self.hook_data.get("matcher", "")
        if matcher:
            self.matcherLabel = QLabel(matcher, self)
            self.matcherLabel.setStyleSheet(
                f"background-color: #E0E0E0; color: #666; {get_font_family_css()} font-size: {scale_font_size(11)}px; padding: 2px 6px; border-radius: 4px;"
            )
        else:
            self.matcherLabel = QLabel("")
        
        # 启用开关
        self.switch = SwitchButton(self)
        SwitchStyles.configure(self.switch)
        self.switch.setChecked(self.hook_data.get("enabled", True))
        
        # 编辑 / 删除按钮
        self.editBtn = ToolButton(FluentIcon.EDIT)
        self.editBtn.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        self.editBtn.setStyleSheet(ButtonStyles.tool_button())
        self.editBtn.clicked.connect(lambda: self.edited.emit(self.index))
        
        self.delBtn = ToolButton(FluentIcon.CLOSE)
        self.delBtn.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        self.delBtn.setStyleSheet(ButtonStyles.tool_button())
        self.delBtn.clicked.connect(lambda: self.removed.emit(self.index))
        
        self.setFixedHeight(40)
        self.hBoxLayout.setContentsMargins(48, 0, 16, 0)
        self.hBoxLayout.addWidget(self.commandLabel, 1)
        self.hBoxLayout.addWidget(self.matcherLabel, 0)
        self.hBoxLayout.addSpacing(12)
        self.hBoxLayout.addWidget(self.switch, 0)
        self.hBoxLayout.addWidget(self.editBtn, 0)
        self.hBoxLayout.addWidget(self.delBtn, 0)
        self.hBoxLayout.setAlignment(Qt.AlignVCenter)
        
        self.switch.checkedChanged.connect(lambda checked: self.toggled.emit(self.index, checked))


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
        self.eventCombo = SearchableEditableComboBox()
        self.eventCombo.addItems([
            "SessionStart", "PreUserMessage", "PostUserMessage",
            "PreAssistantMessage", "PostAssistantMessage",
            "PreToolUse", "PostToolUse"
        ])
        row, _ = _make_row("事件:", self.eventCombo)
        main_layout.addLayout(row)

        # ── 类型 ──
        self.typeCombo = SearchableEditableComboBox()
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
        self.commandEdit.setText(d.get("command", d.get("url", "")))
        self.matcherEdit.setText(d.get("matcher", ""))
    
    def get_values(self) -> dict:
        return {
            "event": self.eventCombo.currentText(),
            "type": self.typeCombo.currentText(),
            "command": self.commandEdit.text().strip(),
            "matcher": self.matcherEdit.text().strip(),
            "enabled": True
        }
    
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
    showEditHookCard = pyqtSignal(str, dict)  # 显示编辑 Hook 卡片: (event_name, hook_data)
    
    def __init__(self, icon: QIcon, title: str, content: str = None, parent=None, home=None,
                 hook_manager=None):
        self.home = home
        self._hook_manager = hook_manager
        super().__init__(icon, title, content, parent)
        self.title = title
        self.all_hooks = {}
        self.hooks_config_file = get_app_data_dir() / "hooks" / "hooks.json"
        self._setup_ui()
        self._refresh()
    
    def _load_hooks(self):
        """从 HookManager 加载所有 hooks（文件 hooks + 技能 hooks），转为规则格式"""
        self.all_hooks = {"SessionStart": [], "PreUserMessage": [], "PostUserMessage": [],
                          "PreAssistantMessage": [], "PostAssistantMessage": [],
                          "PreToolUse": [], "PostToolUse": []}
        
        if not self._hook_manager:
            return
        
        # 获取所有 hooks（扁平格式）
        hm_hooks = self._hook_manager.get_all_hooks()
        global_config = str(self.hooks_config_file)
        
        # 计算 global_config 的 resolved 路径（一次性）
        try:
            global_resolved = Path(global_config).resolve()
        except Exception:
            global_resolved = Path(global_config)
        
        for event_name, flat_hooks in hm_hooks.items():
            if event_name not in self.all_hooks:
                self.all_hooks[event_name] = []
            
            # 按 (matcher, is_skill) 分组：skill hook 和 global hook 即使 matcher 相同也不合并
            rule_map = {}  # (matcher, is_skill) -> list of hook dicts
            for hook in flat_hooks:
                matcher = hook.get("matcher", "")
                is_skill = self._is_skill_hook(hook, global_resolved)
                key = (matcher, is_skill)
                if key not in rule_map:
                    rule_map[key] = []
                rule_map[key].append(hook)
            
            for (matcher, is_skill), hooks in rule_map.items():
                rule_entry = {
                    "matcher": matcher,
                    "hooks": hooks,
                }
                if is_skill:
                    rule_entry["_readonly"] = True
                self.all_hooks[event_name].append(rule_entry)
    
    def _is_skill_hook(self, hook: dict, global_resolved: Path) -> bool:
        """判断 hook 是否来自技能目录（而非全局配置文件）"""
        cf = hook.get("config_file")
        if not cf:
            return False  # 没有 config_file 的当作全局 hook
        try:
            cf_resolved = Path(cf).resolve()
            return cf_resolved != global_resolved
        except Exception:
            return str(self.hooks_config_file) not in cf  # fallback: 字符串包含判断
    
    def _save_hooks(self):
        """保存全局 hooks 到配置文件，并同步到 HookManager"""
        # 过滤掉 _readonly 的 skill hooks
        save_data = {}
        for event_name, rules in self.all_hooks.items():
            filtered_rules = [r for r in rules if not r.get("_readonly", False)]
            if filtered_rules:
                save_data[event_name] = filtered_rules
        
        self.hooks_config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.hooks_config_file, 'w', encoding='utf-8') as f:
            json.dump({"hooks": save_data}, f, indent=2, ensure_ascii=False)
        
        # 同步到 HookManager（热重载）
        if self._hook_manager:
            self._hook_manager.reload_global_hooks(str(self.hooks_config_file))
    
    def _setup_ui(self):
        self.viewLayout.setSpacing(0)
        self.viewLayout.setAlignment(Qt.AlignTop)
        self.viewLayout.setContentsMargins(8, 0, 8, 0)
        
        self.addButton = PushButton("添加", self, FluentIcon.ADD)
        self.refreshButton = PushButton("刷新", self, FluentIcon.SYNC)
        self.addButton.setObjectName("_hook_add_btn")
        self.refreshButton.setObjectName("_hook_refresh_btn")
        
        self.addButton.clicked.connect(self.showAddHookCard.emit)
        self.refreshButton.clicked.connect(self._refresh)
        
        self.addWidget(self.addButton)
        self.addWidget(self.refreshButton)
        
        self._update_button_position()
        
        # 事件分组
        self._render_hooks()
    
    def _update_button_position(self):
        """将 addButton + refreshButton 移到卡片头部 expandButton 左侧"""
        card = self.card
        if not hasattr(card, 'hBoxLayout'):
            return
        # 先从原始位置移除
        card.hBoxLayout.removeWidget(self.addButton)
        card.hBoxLayout.removeWidget(self.refreshButton)
        # 找到 expandButton 位置，在其前面插入
        for i in range(card.hBoxLayout.count()):
            item = card.hBoxLayout.itemAt(i)
            if item.widget() == card.expandButton:
                card.hBoxLayout.removeItem(card.hBoxLayout.itemAt(i - 1))
                card.hBoxLayout.insertWidget(i - 1, self.refreshButton, 0, Qt.AlignRight)
                card.hBoxLayout.insertSpacing(i - 1, 4)
                card.hBoxLayout.insertWidget(i - 1, self.addButton, 0, Qt.AlignRight)
                card.hBoxLayout.insertSpacing(i - 1, 4)
                card.hBoxLayout.insertSpacing(i + 3, 4)
                break
    
    def _render_hooks(self):
        """渲染所有 hooks（数据已统一为规则格式）"""
        has_visible = any(rules for event_name, rules in self.all_hooks.items() if rules)
        if not has_visible:
            empty_label = QLabel("暂无 Hooks，点击「+ 添加」创建", self.view)
            empty_label.setStyleSheet(f"color: #888; {get_font_family_css()} font-size: {scale_font_size(12)}px; padding: 16px;")
            empty_label.setAlignment(Qt.AlignCenter)
            self.viewLayout.addWidget(empty_label)
            return
        
        for event_name, rules in self.all_hooks.items():
            if not rules:
                continue
            
            # 数据已统一为规则格式: [{"matcher": "...", "hooks": [...]}, ...]
            # 事件标题
            header = QLabel(f"Event: {event_name}", self.view)
            header.setStyleSheet(
                f"background-color: #F0F0F0; color: #333; font-weight: bold; "
                f"{get_font_family_css()} font-size: {scale_font_size(12)}px; padding: 6px 48px;"
            )
            self.viewLayout.addWidget(header)
            
            # Hook 条目
            for rule_index, rule in enumerate(rules):
                hooks = rule.get("hooks", [])
                for hook_index, hook in enumerate(hooks):
                    item = HookItem(event_name, hook, hook_index, self.view)
                    # 只读 hooks：允许开关操作，但持久化到技能自己的配置文件
                    is_readonly = rule.get("_readonly", False)
                    # 使用闭包捕获正确的变量值
                    item.removed.connect(
                        lambda idx, en=event_name, ri=rule_index: self._remove_hook(en, ri, idx)
                    )
                    item.edited.connect(
                        lambda idx, en=event_name, ri=rule_index, hi=hook_index, hk=hook, rl=rule: self._edit_hook(en, ri, hi, hk, rl)
                    )
                    item.toggled.connect(
                        lambda idx, enabled, en=event_name, ri=rule_index, hi=hook_index: self._toggle_hook(en, ri, hi, enabled)
                    )
                    self.viewLayout.addWidget(item)
    
    def _get_hook_index(self, event_name, rule_index, hook_index):
        """计算 hook 在事件内的索引（与 HookManager.set_hook_enabled 的事件内索引保持一致）"""
        total = 0
        rules = self.all_hooks.get(event_name, [])
        for ri, rule in enumerate(rules):
            for hi, hook in enumerate(rule.get("hooks", [])):
                if ri == rule_index and hi == hook_index:
                    return total
                total += 1
        return -1
    
    def _add_hook(self, event: str, command: str, matcher: str = "", hook_type: str = "command", enabled: bool = True):
        """添加新 hook（直接修改 self.all_hooks 并持久化）"""
        if event not in self.all_hooks:
            self.all_hooks[event] = []
        
        hook_data = {
            "type": hook_type,
            "command": command,
            "enabled": enabled
        }
        
        new_rule = {"hooks": [hook_data]}
        if matcher:
            new_rule["matcher"] = matcher
        
        self.all_hooks[event].append(new_rule)
        
        # 先保存到文件，再同步到 HookManager
        self._save_hooks()
        
        self._refresh()
        self.hooksChanged.emit()

    def _update_hook(self, original_event: str, original_command: str, original_matcher: str, new_values: dict):
        """更新已有 hook（根据原始信息查找并替换）"""
        # 查找原始 hook 所在规则和位置
        for event_name, rules in list(self.all_hooks.items()):
            for ri, rule in enumerate(rules):
                if rule.get("_readonly", False):
                    continue
                rule_matcher = rule.get("matcher", "")
                hooks = rule.get("hooks", [])
                for hi, hook in enumerate(hooks):
                    if (hook.get("command", "") == original_command
                            and event_name == original_event
                            and rule_matcher == original_matcher):
                        event = new_values.get("event", original_event)
                        command = new_values.get("command", "")
                        matcher = new_values.get("matcher", "")
                        hook_type = new_values.get("type", "command")
                        enabled = new_values.get("enabled", True)

                        if event != original_event or matcher != original_matcher:
                            # 事件或 matcher 变了，移动到新位置
                            hooks.pop(hi)
                            if not hooks:
                                rules.pop(ri)
                            if event not in self.all_hooks:
                                self.all_hooks[event] = []
                            new_hook = {"type": hook_type, "command": command, "enabled": enabled}
                            new_rule = {"hooks": [new_hook]}
                            if matcher:
                                new_rule["matcher"] = matcher
                            self.all_hooks[event].append(new_rule)
                        else:
                            hook["type"] = hook_type
                            hook["command"] = command
                            hook["enabled"] = enabled
                        break
                else:
                    continue
                break

        self._save_hooks()
        self._refresh()
        self.hooksChanged.emit()

    def _edit_hook(self, event: str, rule_index: int, hook_index: int, hook: dict, rule: dict):
        """编辑 hook：只读的 skill hook 不可编辑，发出编辑信号"""
        if rule.get("_readonly", False):
            return
        # 将事件名附带到 hook 数据中传递
        hook_with_event = dict(hook)
        hook_with_event["_event"] = event
        self.showEditHookCard.emit(event, hook_with_event)

    def _remove_hook(self, event: str, rule_index: int, hook_index: int):
        """删除 hook（直接修改 self.all_hooks 并持久化）"""
        if event in self.all_hooks:
            rules = self.all_hooks[event]
            if rule_index < len(rules):
                rule = rules[rule_index]
                # 不允许删除只读 skill hooks
                if rule.get("_readonly", False):
                    return
                hooks = rule.get("hooks", [])
                if hook_index < len(hooks):
                    hooks.pop(hook_index)
                    # 如果规则没有 hooks 了，移除规则
                    if not hooks:
                        rules.pop(rule_index)
                    # 如果事件没有规则了，移除事件
                    if not rules:
                        del self.all_hooks[event]
                    
                    # 先保存到文件，再同步到 HookManager
                    self._save_hooks()
                    
                    self._refresh()
                    self.hooksChanged.emit()
    
    def _toggle_hook(self, event: str, rule_index: int, hook_index: int, enabled: bool):
        """切换 hook 启用状态（直接修改 self.all_hooks 并持久化）"""
        if event in self.all_hooks:
            rules = self.all_hooks[event]
            if rule_index < len(rules):
                rule = rules[rule_index]
                hooks = rule.get("hooks", [])
                if hook_index < len(hooks):
                    hooks[hook_index]["enabled"] = enabled
                    
                    if rule.get("_readonly", False):
                        # 技能 hook：通过 HookManager 保存到技能的 config_file
                        hook_event_index = self._get_hook_index(event, rule_index, hook_index)
                        if self._hook_manager and hook_event_index >= 0:
                            self._hook_manager.set_hook_enabled(event, hook_event_index, enabled)
                    else:
                        # 全局 hook：保存到全局配置文件
                        self._save_hooks()
                    
                    self.hooksChanged.emit()
    
    def _refresh(self):
        """刷新 hook 列表（保留添加/刷新按钮和展开状态）"""
        was_expanded = self.isExpand
        
        self._load_hooks()
        # 稳妥方式清空 viewLayout：takeAt + 删除 widget
        while self.viewLayout.count():
            item = self.viewLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        # 重新渲染
        self._render_hooks()
        
        # 处理异步删除（deleteLater）+ 强制布局计算，确保 sizeHint 正确
        from PyQt5.QtCore import QCoreApplication
        QCoreApplication.processEvents()
        self.viewLayout.activate()
        self.view.updateGeometry()
        
        # 调整展开区域高度
        self._adjustViewSize()
        # 恢复展开状态：直接强制刷新高度，不用 setExpand（已展开时是空操作）
        if was_expanded:
            h = self.viewLayout.sizeHint().height()
            if h > 0:
                self.setFixedHeight(self.card.height() + h)