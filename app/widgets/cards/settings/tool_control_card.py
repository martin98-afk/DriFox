# -*- coding: utf-8 -*-
"""
工具控制卡片 — 按模块分组控制工具开关，样式对齐模型参数卡片
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
)
from qfluentwidgets import SwitchButton, ComboBox

from app.tools.tool_classifier import (
    classify_tool_danger, get_tool_counts,
    DANGEROUS_TOOLS, SAFE_TOOLS, get_default_toggles,
)
from app.utils.config import Settings
from app.utils.design_tokens import Colors
from app.widgets.cards.settings.system_card_frame import SystemCardFrame


# =============================================================================
# 工具分组定义
# =============================================================================
TOOL_GROUPS = [
    ("📁 文件写入", ["write", "edit", "multi_edit"]),
    ("💻 终端命令", ["bash", "bg_start", "bg_stop"]),
    ("🖱 桌面控制", ["mouse", "keyboard"]),
    ("☁️ 文件上传", ["upload_file"]),
    ("📝 状态修改", ["edit_project_note", "todowrite", "stage_files"]),
    ("🤖 子智能体", ["subagent_para", "subagent_dag"]),
    ("✅ 安全操作", sorted(SAFE_TOOLS)),
]

TOOL_DESCRIPTIONS = {
    "write": "覆盖/创建文件",
    "edit": "精确文本替换",
    "multi_edit": "批量文件编辑",
    "bash": "执行shell命令",
    "bg_start": "启动后台命令",
    "bg_stop": "停止后台任务",
    "mouse": "鼠标操作",
    "keyboard": "键盘操作",
    "upload_file": "上传到Gitee",
    "edit_project_note": "编辑项目笔记",
    "todowrite": "创建/更新待办",
    "stage_files": "标记相关文件",
    "subagent_para": "并行启动子智能体",
    "subagent_dag": "DAG工作流子智能体",
}

OFF_BEHAVIOR_OPTIONS = [
    ("deny", "直接拒绝"),
    ("ask", "询问用户"),
]


class ToolControlCardContent(QWidget):
    """工具控制卡片内容 — 分组折叠 + 独立开关"""

    togglesChanged = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = Settings.get_instance()
        self._toggles: dict = {}
        self._toggle_widgets: dict = {}
        self._group_switches: dict = {}
        self._setup_ui()

    def _setup_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 4, 0, 4)
        self._layout.setSpacing(8)

    def set_toggles(self, toggles: dict):
        self._toggles = dict(toggles)
        self._rebuild()

    def get_toggles(self) -> dict:
        return dict(self._toggles)

    def _rebuild(self):
        """全量重建内容（仅首次加载调用）"""
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._toggle_widgets.clear()
        self._group_switches.clear()

        # 确保所有工具都在 toggles 中
        all_tools = set(DANGEROUS_TOOLS) | set(SAFE_TOOLS)
        defaults = get_default_toggles(list(all_tools))
        for name in all_tools:
            if name not in self._toggles:
                self._toggles[name] = defaults[name]

        # 按组构建
        for group_name, tool_names in TOOL_GROUPS:
            self._build_group(group_name, tool_names)

        self._layout.addStretch()

    def _refresh_stats(self):
        """仅刷新各整组开关状态（不全量重建）"""
        # 刷新每个组的整组开关状态
        for group_name, tool_names in TOOL_GROUPS:
            gs = self._group_switches.get(group_name)
            if gs:
                all_on = all(self._toggles.get(t, True) for t in tool_names)
                gs.blockSignals(True)
                gs.setChecked(all_on)
                gs.blockSignals(False)

    def _build_group(self, group_name: str, tool_names: list):
        """构建一个工具组"""
        Colors.refresh()
        is_safe = group_name.startswith("✅")
        border_color = "rgba(34,197,94,0.2)" if is_safe else "rgba(255,80,80,0.2)"
        header_bg = "rgba(34,197,94,0.06)" if is_safe else "rgba(255,80,80,0.08)"

        group = QFrame()
        group.setStyleSheet(f"""
            QFrame {{
                background: transparent;
                border: 1px solid {border_color};
                border-radius: 8px;
            }}
        """)
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(0)

        # 组头
        header = QWidget()
        header.setStyleSheet(
            f"background: {header_bg}; border: none; border-radius: 8px;"
        )
        header.setCursor(Qt.PointingHandCursor)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 8, 10, 8)

        label = QLabel(f"{group_name} ({len(tool_names)})")
        label.setStyleSheet(
            "color: #ddd; font-weight: 600; font-size: 12px; background: transparent; border: none;"
        )
        header_layout.addWidget(label)
        header_layout.addStretch()

        # 整组开关
        all_on = all(self._toggles.get(t, True) for t in tool_names)
        group_switch = SwitchButton()
        group_switch.setChecked(all_on)
        group_switch.setFixedSize(38, 20)
        header_layout.addWidget(group_switch)
        self._group_switches[group_name] = group_switch

        group_layout.addWidget(header)

        # 折叠体
        body = QWidget()
        body.setStyleSheet("background: transparent; border: none;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 6, 14, 8)
        body_layout.setSpacing(3)

        for tool_name in tool_names:
            row = self._build_tool_row(tool_name)
            body_layout.addWidget(row)

        group_layout.addWidget(body)
        self._layout.addWidget(group)

        # 点击组头切换折叠
        header.mousePressEvent = lambda e, b=body: b.setVisible(not b.isVisible())

        # 整组开关连接
        group_switch.checkedChanged.connect(
            lambda checked, names=tool_names: self._on_group_toggled(names, checked)
        )

        # 安全组默认折叠
        if is_safe:
            body.setVisible(False)

    def _build_tool_row(self, tool_name: str) -> QWidget:
        """构建单个工具行"""
        row = QWidget()
        row.setStyleSheet("background: transparent; border: none;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 3, 0, 3)
        row_layout.setSpacing(8)

        name_label = QLabel(tool_name)
        name_label.setStyleSheet(
            "color: #ccc; font-size: 12px; background: transparent; border: none;"
        )
        name_label.setFixedWidth(80)
        row_layout.addWidget(name_label)

        desc = TOOL_DESCRIPTIONS.get(tool_name, "")
        desc_label = QLabel(desc)
        desc_label.setStyleSheet(
            "color: #666; font-size: 10px; background: transparent; border: none;"
        )
        desc_label.setMaximumWidth(90)
        row_layout.addWidget(desc_label)
        row_layout.addStretch()

        enabled = self._toggles.get(tool_name, True)
        sw = SwitchButton()
        sw.setChecked(enabled)
        sw.setFixedSize(34, 16)
        row_layout.addWidget(sw)
        self._toggle_widgets[tool_name] = sw

        sw.checkedChanged.connect(
            lambda checked, name=tool_name: self._on_tool_toggled(name, checked)
        )

        return row

    def _on_tool_toggled(self, tool_name: str, enabled: bool):
        self._toggles[tool_name] = enabled
        self._settings.tool_toggles.value = dict(self._toggles)
        self._settings.save()
        self._refresh_stats()
        self.togglesChanged.emit(dict(self._toggles))

    def _on_group_toggled(self, tool_names: list, enabled: bool):
        for name in tool_names:
            self._toggles[name] = enabled
            tw = self._toggle_widgets.get(name)
            if tw:
                tw.blockSignals(True)
                tw.setChecked(enabled)
                tw.blockSignals(False)
        self._settings.tool_toggles.value = dict(self._toggles)
        self._settings.save()
        self._refresh_stats()
        self.togglesChanged.emit(dict(self._toggles))

    def show_content(self):
        self._toggles = dict(self._settings.tool_toggles.value)
        self._rebuild()

    def hide_content(self):
        pass


class ToolControlCardFrame(SystemCardFrame):
    """工具控制卡片框架 — SystemCardFrame 包裹"""

    togglesChanged = pyqtSignal(dict)
    behaviorChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.set_height_mode("proportional")
        self.setMinimumHeight(250)

        self.title_label.setText("🔧 工具控制")
        self.icon_label.hide()

        # 右上角下拉框：关闭时行为
        self._behavior_combo = ComboBox(self)
        self._behavior_combo.setFixedWidth(100)
        for value, label in OFF_BEHAVIOR_OPTIONS:
            self._behavior_combo.addItem(label, userData=value)
        current = Settings.get_instance().tool_off_behavior.value
        idx = self._behavior_combo.findData(current)
        if idx >= 0:
            self._behavior_combo.setCurrentIndex(idx)
        self._behavior_combo.currentIndexChanged.connect(self._on_behavior_changed)

        self._header_layout.insertWidget(
            self._header_layout.count() - 2, self._behavior_combo
        )

        self._card = ToolControlCardContent(self)
        self._content_layout.addWidget(self._card)
        # 增大内容区水平边距，防止 SwitchButton 被卡片边框裁剪
        self._content_layout.setContentsMargins(8, 2, 8, 2)

        self._card.togglesChanged.connect(self.togglesChanged.emit)
        self._card.togglesChanged.connect(lambda t: self._refresh_stats(t))

    def _on_behavior_changed(self, idx: int):
        value = self._behavior_combo.itemData(idx)
        Settings.get_instance().tool_off_behavior.value = value
        Settings.get_instance().save()
        self.behaviorChanged.emit(value)

    def _refresh_stats(self, toggles: dict):
        dangerous, safe = get_tool_counts(toggles)
        self._count_label.setText(f"{dangerous}危险·{safe}安全")
        self._count_label.setVisible(True)

    def set_toggles(self, toggles: dict):
        self._card.set_toggles(toggles)

    def get_toggles(self) -> dict:
        return self._card.get_toggles()

    def show_card(self):
        self._card.show_content()
        self.setVisible(True)

    def hide_card(self):
        self._card.hide_content()
        self.setVisible(False)
