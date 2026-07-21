# -*- coding: utf-8 -*-
"""
工具控制卡片 — 按模块分组控制工具开关,样式对齐模型参数卡片

数据源:ToolPermissionController(per-window,多窗口隔离)
- 卡片显示 controller 的 active_tool_toggles(智能体激活时显示 agent 权限)
- 用户编辑写入 user_tool_toggles(智能体模式下不影响 active)
- "↺ 恢复"按钮调用 controller.restore_user()
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import ComboBox, SwitchButton

from app.tools.tool_classifier import (
    DANGEROUS_TOOLS,
    SAFE_TOOLS,
    get_default_toggles,
)
from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.cards.settings.system_card_frame import SystemCardFrame
from app.widgets.elided_label import _ElidedLabel

# =============================================================================
# 工具分组定义
# =============================================================================
TOOL_GROUPS = [
    ("📁 文件写入", ["write", "edit", "multi_edit"]),
    ("💻 终端命令", ["bash", "bg_start", "bg_stop"]),
    ("🖱 桌面控制", ["mouse", "keyboard"]),
    ("☁️ 文件上传", ["upload_file"]),
    ("📝 状态修改", ["todowrite", "stage_files"]),
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
    "upload_file": "上传本地文件到Gitee",
    
    "todowrite": "创建/更新待办",
    "stage_files": "标记相关文件",
    "subagent_para": "并行启动子智能体",
    "subagent_dag": "DAG工作流子智能体",
    # 文件与信息检索
    "read": "读取文件内容",
    "grep": "正则搜索文件内容",
    "list": "列出目录内容",
    "glob": "通配符查找文件",
    "scan_repo": "扫描仓库生成摘要",
    "webfetch": "获取网页内容",
    "websearch": "网络关键词搜索",
    # 后台任务与系统
    "bg_logs": "查看后台任务日志",
    "bg_list": "列出后台任务状态",
    "screenshot": "截取屏幕截图",
    "get_diagnostics": "获取代码诊断信息",
    # 待办
    "todoread": "读取待办列表",
    # 交互与元工具
    "question": "向用户提问确认",
    "skill": "加载指定技能",
    "list_skills": "列出可用技能",
    "subagent_status": "查询子智能体状态",
    "mcp_list_servers": "列出MCP服务器",
    "lsp": "LSP代码智能操作",
    # CodeGraph 代码智能
    "codegraph_explore": "语义级代码探索",
}

OFF_BEHAVIOR_OPTIONS = [
    ("deny", "直接拒绝"),
    ("ask", "询问用户"),
]


class ToolControlCardContent(QWidget):
    """工具控制卡片内容 — 分组折叠 + 独立开关"""

    togglesChanged = pyqtSignal(dict)

    def __init__(self, parent=None, controller=None):
        super().__init__(parent)
        self._controller = controller  # ToolPermissionController
        self._toggle_widgets: dict = {}
        self._group_switches: dict = {}
        self._group_labels: dict = {}  # group_name -> (QLabel, tool_names) 用于刷新"启用数/总数"
        self._built = False  # 首次 show_content 才构建,避免启动即耗 CPU
        self._setup_ui()

        if self._controller:
            self._bind_controller(self._controller)

    def set_controller(self, controller):
        """延迟绑定 controller(main_widget 在 super().__init__ 之后注入时使用)"""
        self._controller = controller
        if controller:
            self._bind_controller(controller)

    def _bind_controller(self, controller):
        """连接 controller 信号,初始化 UI

        注意:不立即 _rebuild(),因为此时 widget 还没显示。
        等首次 show_content() 时再构建(惰性渲染)。
        期间 controller 信号已连接,_toggle_widgets 为空时 _apply_toggles 是 no-op,
        不会丢更新 — 首次 _rebuild 会从 controller 拉最新数据。
        """
        controller.togglesChanged.connect(self._on_active_toggles_changed)
        controller.behaviorChanged.connect(self._on_active_behavior_changed)
        controller.activeAgentChanged.connect(lambda _: self._on_active_agent_changed())

    def _on_active_toggles_changed(self, toggles):
        """controller 通知 active toggles 变化(智能体激活/恢复/用户编辑)"""
        from loguru import logger
        agent_name = self._controller.get_active_agent_name() if self._controller else None
        enabled = sum(1 for v in toggles.values() if v)
        logger.info(f"[ToolCard] togglesChanged: agent={agent_name}, enabled={enabled}/{len(toggles)}")
        self._apply_toggles()
        # 向上层转发,触发主窗口工具栏按钮数字刷新
        self.togglesChanged.emit(toggles)

    def _on_active_behavior_changed(self, _behavior):
        """controller 通知 active behavior 变化,转发 togglesChanged 让工具栏刷新"""
        if self._controller:
            self.togglesChanged.emit(self._controller.get_toggles())

    def _setup_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 4, 0, 4)
        self._layout.setSpacing(8)

    def _on_active_agent_changed(self):
        """智能体激活状态变化时,刷新开关为 agent 的 active 值"""
        self._apply_toggles()

    def refresh(self):
        """从 controller 强制刷新 UI(供 main_widget 在关键节点主动调用)

        未构建时不做事 — 等首次 show_content 时会从 controller 拉最新数据。
        """
        if self._controller is None or not self._built:
            return
        self._apply_toggles()

    def refresh_style(self):
        """主题/字体变更时重建全部 widget 以应用新样式

        未构建时跳过 — 等下次 show_content 时会自动重建以应用新样式。
        """
        if not self._built:
            return
        self._rebuild()

    def _rebuild(self):
        """全量重建内容"""
        from loguru import logger
        self._built = True  # 标记已构建
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._toggle_widgets.clear()
        self._group_switches.clear()
        self._group_labels.clear()

        # 从 controller 获取当前生效的 toggles
        if self._controller:
            toggles = self._controller.get_toggles()
            agent = self._controller.get_active_agent_name()
            logger.info(f"[ToolCard] _rebuild: agent={agent}, toggles_enabled={sum(1 for v in toggles.values() if v)}/{len(toggles)}")
        else:
            toggles = {}
            logger.info("[ToolCard] _rebuild: controller=None!")

        # 确保所有工具都在 toggles 中
        all_tools = set(DANGEROUS_TOOLS) | set(SAFE_TOOLS)
        defaults = get_default_toggles(list(all_tools))
        for name in all_tools:
            if name not in toggles:
                toggles[name] = defaults[name]

        # 按组构建
        for group_name, tool_names in TOOL_GROUPS:
            self._build_group(group_name, tool_names, toggles)

        self._layout.addStretch()

    def _apply_toggles(self):
        """轻量级更新所有开关状态,不全量重建 widget"""
        if not self._controller:
            return
        toggles = self._controller.get_toggles()

        # 更新单个工具开关
        for tool_name, sw in self._toggle_widgets.items():
            enabled = toggles.get(tool_name, True)
            if sw.isChecked() != enabled:
                sw.blockSignals(True)
                sw.setChecked(enabled)
                sw.blockSignals(False)

        # 更新整组开关 + 组标题"启用数/总数"
        for group_name, tool_names in TOOL_GROUPS:
            gs = self._group_switches.get(group_name)
            if gs:
                all_on = all(toggles.get(t, True) for t in tool_names)
                if gs.isChecked() != all_on:
                    gs.blockSignals(True)
                    gs.setChecked(all_on)
                    gs.blockSignals(False)
            label_info = self._group_labels.get(group_name)
            if label_info:
                label, names = label_info
                enabled_count = sum(1 for t in names if toggles.get(t, True))
                new_text = f"{group_name} ({enabled_count}/{len(names)})"
                if label.text() != new_text:
                    label.setText(new_text)

    def _refresh_stats(self):
        """仅刷新各整组开关状态(不全量重建)"""
        if not self._controller:
            return
        toggles = self._controller.get_toggles()
        for group_name, tool_names in TOOL_GROUPS:
            gs = self._group_switches.get(group_name)
            if gs:
                all_on = all(toggles.get(t, True) for t in tool_names)
                gs.blockSignals(True)
                gs.setChecked(all_on)
                gs.blockSignals(False)

    def _build_group(self, group_name: str, tool_names: list, all_toggles: dict):
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

        enabled_count = sum(1 for t in tool_names if all_toggles.get(t, True))
        label = QLabel(f"{group_name} ({enabled_count}/{len(tool_names)})")
        label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-weight: 600; background: transparent; border: none; "
            f"{font_size_css(12)} {get_font_family_css()}"
        )
        header_layout.addWidget(label)
        header_layout.addStretch()
        self._group_labels[group_name] = (label, list(tool_names))

        # 整组开关
        all_on = all(all_toggles.get(t, True) for t in tool_names)
        group_switch = SwitchButton()
        group_switch.setChecked(all_on)
        header_layout.addWidget(group_switch)
        header_layout.addSpacing(12)
        self._group_switches[group_name] = group_switch

        group_layout.addWidget(header)

        # 折叠体
        body = QWidget()
        body.setStyleSheet("background: transparent; border: none;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 6, 14, 8)
        body_layout.setSpacing(3)

        for tool_name in tool_names:
            row = self._build_tool_row(tool_name, all_toggles)
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

    def _build_tool_row(self, tool_name: str, all_toggles: dict) -> QWidget:
        """构建单个工具行"""
        row = QWidget()
        row.setStyleSheet("background: transparent; border: none;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 3, 0, 3)
        row_layout.setSpacing(8)

        enabled = all_toggles.get(tool_name, True)

        name_label = QLabel(tool_name)
        name_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none; "
            f"{font_size_css(12)} {get_font_family_css()}"
        )
        row_layout.addWidget(name_label)

        desc = TOOL_DESCRIPTIONS.get(tool_name, "")
        desc_label = _ElidedLabel(desc)
        desc_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        desc_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; background: transparent; border: none; "
            f"{font_size_css(10)} {get_font_family_css()}"
        )
        row_layout.addWidget(desc_label)

        sw = SwitchButton()
        sw.setChecked(enabled)
        row_layout.addWidget(sw)
        self._toggle_widgets[tool_name] = sw

        sw.checkedChanged.connect(
            lambda checked, name=tool_name: self._on_tool_toggled(name, checked)
        )

        return row

    def _on_tool_toggled(self, tool_name: str, enabled: bool):
        """用户编辑单个开关
        - 非 agent 模式:user 和 active 同步更新(并持久化)
        - agent 模式:只更新 active(临时改 agent 生效权限,user 偏好不变)
        """
        from loguru import logger
        if self._controller is None:
            logger.warning(f"[ToolCard] _on_tool_toggled({tool_name},{enabled}) skipped: controller=None")
            return
        logger.info(f"[ToolCard] _on_tool_toggled: {tool_name}={enabled}, agent_active={self._controller.is_agent_active()}")
        # 直接更新 controller(触发信号链供外部使用)
        self._controller.set_user_toggle(tool_name, enabled)
        # 轻量级刷新 UI（controller 信号也会触发 _apply_toggles，但开销极低）
        self._apply_toggles()
        # 通知 frame 刷新统计
        self.togglesChanged.emit(self._controller.get_toggles())

    def _on_group_toggled(self, tool_names: list, enabled: bool):
        """用户编辑整组开关
        - 非 agent 模式:user 和 active 同步
        - agent 模式:只改 active
        """
        if self._controller is None:
            return
        new_toggles = {name: enabled for name in tool_names}
        self._controller.set_user_toggles(new_toggles)
        # 轻量级刷新 UI
        self._apply_toggles()
        self.togglesChanged.emit(self._controller.get_toggles())

    def show_content(self):
        """卡片显示时刷新(从 controller 拉取最新状态)

        惰性构建:首次显示才全量 _rebuild(),后续走 _apply_toggles 轻量更新。
        避免每次打开卡片都重建 33 个 SwitchButton 导致 ~200ms 卡顿。
        """
        if not self._built:
            self._rebuild()
        else:
            self._apply_toggles()

    def hide_content(self):
        pass


class ToolControlCardFrame(SystemCardFrame):
    """工具控制卡片框架 — SystemCardFrame 包裹"""

    togglesChanged = pyqtSignal(dict)
    behaviorChanged = pyqtSignal(str)

    def __init__(self, parent=None, controller=None):
        super().__init__(parent)
        self._controller = controller
        self.set_height_mode("proportional")
        self.setMinimumHeight(250)

        self.title_label.setText("工具控制")
        # 使用主题感知 SVG 图标代替 emoji
        self.icon_label.show()
        self.icon_label.setPixmap(get_icon("工具").pixmap(18, 18))
        self.icon_label.setFixedSize(18, 18)

        # ========== 右上角下拉框:关闭时行为 ==========
        self._behavior_combo = ComboBox(self)
        for value, label in OFF_BEHAVIOR_OPTIONS:
            self._behavior_combo.addItem(label, userData=value)
        # 从 controller 读取当前 behavior
        current_behavior = (
            self._controller.get_behavior() if self._controller else "deny"
        )
        idx = self._behavior_combo.findData(current_behavior)
        if idx >= 0:
            self._behavior_combo.setCurrentIndex(idx)
        self._behavior_combo.currentIndexChanged.connect(self._on_behavior_changed)

        # ========== 智能体徽章 + 恢复按钮(仅 agent 激活时显示) ==========
        self._active_agent_label = QLabel(self)
        self._active_agent_label.setStyleSheet(
            f"color: #ff9500; font-weight: 600; "
            f"background: rgba(255,149,0,0.12); border: 1px solid rgba(255,149,0,0.3); "
            f"border-radius: 6px; padding: 2px 8px; {font_size_css(12)} {get_font_family_css()}"
        )
        self._active_agent_label.setVisible(False)
        self._active_agent_label.setToolTip(
            "当前工具权限由智能体命令注入,点击「恢复」可回到用户设置"
        )

        self._restore_btn = QPushButton("↺ 恢复", self)
        self._restore_btn.setFixedHeight(26)
        self._restore_btn.setCursor(Qt.PointingHandCursor)
        self._restore_btn.setStyleSheet(
            f"QPushButton {{"
            f"  color: #fff; background: rgba(255,149,0,0.85); "
            f"  border: none; border-radius: 6px; padding: 2px 10px; {font_size_css(12)} {get_font_family_css()}"
            f"}}"
            f"QPushButton:hover {{ background: rgba(255,149,0,1.0); }}"
            f"QPushButton:pressed {{ background: rgba(255,149,0,0.7); }}"
        )
        self._restore_btn.setVisible(False)
        self._restore_btn.setToolTip("恢复用户自定义的工具权限设置")
        self._restore_btn.clicked.connect(self._on_restore_clicked)

        # ========== 标题栏布局(下拉框 → 徽章 → 恢复按钮) ==========
        insert_idx = max(0, self._header_layout.count() - 2)
        self._header_layout.insertWidget(insert_idx, self._behavior_combo)
        self._header_layout.insertWidget(insert_idx + 1, self._active_agent_label)
        self._header_layout.insertWidget(insert_idx + 2, self._restore_btn)

        # ========== 内容区 ==========
        self._card = ToolControlCardContent(self, controller)
        self._content_layout.addWidget(self._card)
        # 增大内容区水平边距,防止 SwitchButton 被卡片边框裁剪
        self._content_layout.setContentsMargins(8, 2, 8, 2)

        self._card.togglesChanged.connect(self.togglesChanged.emit)

        # 监听 controller 的智能体激活状态变化
        if self._controller:
            self._controller.activeAgentChanged.connect(self._on_agent_changed)
            # 初始状态
            self._on_agent_changed(self._controller.get_active_agent_name() or "")

    def set_controller(self, controller):
        """延迟绑定 controller(main_widget 在 super().__init__ 之后注入时使用)"""
        self._controller = controller
        if controller is None:
            return
        # 同步 behavior combo 当前值
        idx = self._behavior_combo.findData(controller.get_behavior())
        if idx >= 0:
            self._behavior_combo.setCurrentIndex(idx)
        # 注入到 content
        self._card.set_controller(controller)
        # 监听 controller 信号
        controller.activeAgentChanged.connect(self._on_agent_changed)
        # 初始状态
        self._on_agent_changed(controller.get_active_agent_name() or "")

    def refresh_style(self):
        """主题/字体变更时刷新卡片样式"""
        super().refresh_style()
        # 刷新主题感知图标（浅色/深色切换后更新图标颜色）
        self.icon_label.setPixmap(get_icon("工具").pixmap(18, 18))
        # 刷新内容区 widget（全量重建以应用新样式）
        if hasattr(self, "_card") and self._card is not None:
            self._card.refresh_style()
        self.update()

    def _on_behavior_changed(self, idx: int):
        value = self._behavior_combo.itemData(idx)
        if self._controller:
            self._controller.set_user_behavior(value)
        self.behaviorChanged.emit(value)

    def _on_restore_clicked(self):
        """用户点击"恢复"按钮"""
        if self._controller:
            self._controller.restore_user()

    def refresh(self):
        """从 controller 强制刷新整个卡片(供 main_widget 在关键节点主动调用)"""
        if self._controller is None:
            return
        # 刷新 content
        if hasattr(self, "_card") and self._card is not None:
            self._card.refresh()
        # 刷新徽章和恢复按钮显示
        self._on_agent_changed(self._controller.get_active_agent_name() or "")
        # 刷新 behavior combo
        idx = self._behavior_combo.findData(self._controller.get_behavior())
        if idx >= 0 and idx != self._behavior_combo.currentIndex():
            self._behavior_combo.blockSignals(True)
            self._behavior_combo.setCurrentIndex(idx)
            self._behavior_combo.blockSignals(False)
        self.update()

    def _on_agent_changed(self, agent_name: str):
        """智能体激活状态变化时,显示/隐藏徽章和恢复按钮"""
        if agent_name:
            self._active_agent_label.setText(f"🤖 {agent_name}")
            self._active_agent_label.setVisible(True)
            self._restore_btn.setVisible(True)
        else:
            self._active_agent_label.setVisible(False)
            self._restore_btn.setVisible(False)

    def set_toggles(self, toggles: dict):
        """兼容旧 API:仅用于初始化占位,实际数据来自 controller"""
        if self._controller:
            self._card.show_content()
        else:
            self._card._rebuild()  # 兜底

    def get_toggles(self) -> dict:
        if self._controller:
            return self._controller.get_toggles()
        return {}

    def show_card(self):
        self._card.show_content()
        self.setVisible(True)

    def hide_card(self):
        self._card.hide_content()
        self.setVisible(False)
