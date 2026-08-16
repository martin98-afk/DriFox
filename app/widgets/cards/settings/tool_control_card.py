# -*- coding: utf-8 -*-
"""
工具控制卡片 — 按模块分组控制工具开关,样式对齐模型参数卡片

数据源:ToolPermissionController(per-window,多窗口隔离)
- 卡片显示 controller 的 active_tool_toggles(智能体激活时显示 agent 权限)
- 用户编辑写入 user_tool_toggles(智能体模式下不影响 active)
- "↺ 恢复"按钮调用 controller.restore_user()
"""

import time as _time

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import ComboBox, SwitchButton

from app.tools.registry import DANGER_DANGEROUS, DANGER_SAFE, ToolRegistry
from app.tools.tool_classifier import get_all_tools, get_default_toggles
from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.cards.settings.system_card_frame import SystemCardFrame
from app.widgets.elided_label import _ElidedLabel

OFF_BEHAVIOR_OPTIONS = [
    ("deny", "直接拒绝"),
    ("ask", "询问用户"),
]

# 右上角下拉占位项：各工具关闭策略不一致时显示，仅作展示、不可选中
MIXED_OPTION = ("__mixed__", "未统一")

# 工具来源标签样式（参考 hook 配置卡片的 sourceLabel 色块风格）
# - builtin（内置）：灰色 #888
# - system（系统插件）：红色 #e74c3c
# - user（用户插件）：绿色 #2ecc71
_SOURCE_TAG_COLORS = {
    "builtin": ("#888", "内置"),
    "system": ("#e74c3c", "系统"),
    "user": ("#2ecc71", "用户"),
}


# [PROBE] 幽灵窗口排查探针（定位后移除）：记录当前可见的顶层窗口快照。
# 幽灵窗口「空白/半透明小窗口一闪即逝」若来自卡片重建期间弹出的独立窗口
# （ComboBox 下拉菜单 RoundMenu / ToolTip / MaskDialog），此快照可捕获其类型。
def _probe_visible_top_windows(tag: str):
    from loguru import logger

    try:
        wins = []
        for w in QApplication.topLevelWidgets():
            if not w.isVisible():
                continue
            cls = type(w).__name__
            title = ""
            try:
                title = w.windowTitle()[:24]
            except Exception:
                pass
            wins.append(f"{cls}({title})" if title else cls)
        logger.info(f"[ToolCard][probe] {tag}: top_windows={wins}")
    except Exception as e:
        logger.warning(f"[ToolCard][probe] {tag} failed: {e}")


def _format_source_label(source: str, plugin_root_kind: str = "") -> tuple:
    """生成来源标签 (color, text)。

    source 取自 ToolRegistration.source：
    - "builtin"        → 内置
    - "plugin:<name>"  → 直接显示插件名（截断 8 字符）；
                        颜色由 plugin_root_kind 区分（system 红 / user 绿）
    """
    if source == "builtin" or not source:
        return _SOURCE_TAG_COLORS["builtin"]
    plugin_name = source[len("plugin:") :] if source.startswith("plugin:") else ""
    kind = plugin_root_kind if plugin_root_kind in ("system", "user") else "system"
    color, _base_text = _SOURCE_TAG_COLORS[kind]
    if plugin_name:
        display = plugin_name[:8] + ("…" if len(plugin_name) > 8 else "")
        return color, display
    return color, _SOURCE_TAG_COLORS[kind][1]


class ToolControlCardContent(QWidget):
    """工具控制卡片内容 — 分组折叠 + 独立开关"""

    togglesChanged = pyqtSignal(dict)
    _registryChanged = pyqtSignal(int)  # registry 变更桥接（可能来自后台 watcher 线程）

    # version 稳定性重排上限：registry 持续变更（异常场景）时最多重排 50 轮后强制重建
    _REBUILD_RETRY_MAX = 50

    def __init__(self, parent=None, controller=None):
        super().__init__(parent)
        self._controller = controller  # ToolPermissionController
        self._toggle_widgets: dict = {}
        self._policy_combos: dict = {}  # tool_name -> ComboBox(关闭策略,仅关闭时显示)
        self._group_switches: dict = {}
        self._group_labels: dict = {}  # group_name -> (QLabel, tool_names) 用于刷新"启用数/总数"
        self._built = False  # 首次 show_content 才构建,避免启动即耗 CPU
        self._rebuild_pending = False  # 合并同批 registry 变更,避免逐个排队全量重建
        self._queued_built = False  # 排队时刻的 _built 快照(重建与否据此判定)
        self._queued_version = -1  # 排队时刻的 registry version（稳定性合并依据）
        self._rebuild_reschedule = 0  # version 未稳定重排次数（防饿死上限）
        self._needs_rebuild = False  # 隐藏期间收到 registry 变更→延迟到下次显示时重建
        self._groups_cache = []  # 分组缓存（registry version 变化时失效）
        self._groups_cache_version = -1
        self._registryChanged.connect(self._on_registry_changed_ui)  # 后台线程变更→主线程刷新
        self._setup_ui()
        self._bind_registry()

        if self._controller:
            self._bind_controller(self._controller)

    def _bind_registry(self):
        """监听 registry 热更新：工具插件变更时重建卡片（线程安全桥接）"""
        ToolRegistry.get_instance().on_change(self._on_registry_changed)

    def _on_registry_changed(self, version):
        """registry 版本变化（工具插件热插拔/热更新）→ 转发信号到 UI 线程。

        ⚠️ 此回调可能来自后台 watcher 线程（PluginToolWatcher 轮询线程执行
        scan_now，registry.register/unregister 同步 notify 全部 listener）。
        直接 emit 信号：pyqtSignal QueuedConnection 自动排队到 widget 所在
        线程（主线程）执行刷新，避免在后台线程直接操作 Qt 定时器。
        """
        self._registryChanged.emit(version)

    def _on_registry_changed_ui(self, version):
        """主线程槽：去抖合并同批变更，排队一次刷新（重建卡片 + 通知上层计数）。

        一次重扫会「逐个注销 + 全量重注册」全部工具，产生几十次
        change 事件（每次 register/unregister 都 notify）。若每次排队
        全量 _rebuild（~180ms/次）会串行刷屏数秒。pending 标记合并
        同批变更 → 只刷新一次；刷新期间新变更会再触发一次，不丢。

        ★ 性能修复（2026-08-16）：记录排队时的 registry version，
        _do_rebuild 执行时若 version 又变（重扫仍在进行），继续重排等
        稳定——把「一次重扫多次重建」严格收敛为「一次」。日志实证：打开
        卡片后一次后台重扫可触发第二次全量 rebuild（间隔 ~300ms 两次
        ~200ms 重建 = 明显卡顿）。

        无论卡片是否已构建（_built）都要通知上层——主窗口工具栏按钮
        的计数动态读 registry，热重载后必须刷新；否则只有新建窗口
        （重新初始化按钮）才显示正确数量。
        """
        if self._rebuild_pending:
            # 已排队但期间卡片完成构建 → 升级为需要重建（否则变更被吞）
            self._queued_built = self._queued_built or self._built
            self._queued_version = max(self._queued_version, version)
            return
        self._rebuild_pending = True
        self._queued_built = self._built  # 快照排队时刻构建状态
        self._queued_version = version
        QTimer.singleShot(0, self._do_rebuild)

    def _do_rebuild(self):
        """singleShot 回调：重置 pending 后执行刷新（重建卡片 + 通知上层计数）

        重建与否取决于排队时刻的 _built 快照：未构建时跳过重建（首次
        show_content 会从 registry 拉最新数据），但始终通知上层刷新计数。

        ★ 性能修复（2026-08-16）：
        1) registry version 未稳定（重扫仍在进行）→ 重排等稳定，
           一次重扫只重建一次（上限 _REBUILD_RETRY_MAX，防饿死）；
        2) 卡片当前不可见（隐藏/未展开/所在 Tab 非活跃）时**不立即全量
           重建**，仅标记 _needs_rebuild——等下次 show_content 时一次性
           重建。原逻辑下任何 registry 变更（插件热重载，watchfiles 监控
           plugins/ 目录每次保存都触发）都会对「显示过但当前隐藏」的卡片
           执行全量重建（~200ms/张），多 Tab 窗口 × 高频热重载 = 主线程数秒冻结。
        """
        self._rebuild_pending = False
        if self._queued_version >= 0 and self._queued_version != ToolRegistry.get_instance().version():
            if self._rebuild_reschedule >= self._REBUILD_RETRY_MAX:
                # 异常高频变更（持续 50 轮）→ 放弃等待，直接按当前状态重建
                self._rebuild_reschedule = 0
            else:
                self._rebuild_reschedule += 1
                self._rebuild_pending = True
                self._queued_built = self._queued_built or self._built
                self._queued_version = ToolRegistry.get_instance().version()
                QTimer.singleShot(0, self._do_rebuild)
                return
        self._rebuild_reschedule = 0
        if self._queued_built:
            if self.isVisible():
                self._rebuild()
            else:
                self._needs_rebuild = True
        # 通知上层刷新计数（main_widget 工具栏按钮：动态读 registry 显示新数量）
        if self._controller:
            self.togglesChanged.emit(self._controller.get_toggles())

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
        controller.policiesChanged.connect(self._on_active_policies_changed)
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

    def _on_active_policies_changed(self, _policies):
        """controller 通知 per-tool 关闭策略变化,同步行内下拉与可见性"""
        self._apply_toggles()

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

        t0 = _time.monotonic()
        # [PROBE] 重建前后顶层窗口快照：捕获重建期间出现的瞬态独立窗口
        _probe_visible_top_windows("rebuild_start")

        self._built = True  # 标记已构建
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._toggle_widgets.clear()
        self._policy_combos.clear()
        self._group_switches.clear()
        self._group_labels.clear()

        # 从 controller 获取当前生效的 toggles
        if self._controller:
            toggles = self._controller.get_toggles()
            agent = self._controller.get_active_agent_name()
            logger.info(
                f"[ToolCard] _rebuild: agent={agent}, toggles_enabled={sum(1 for v in toggles.values() if v)}/{len(toggles)}"
            )
        else:
            toggles = {}
            logger.info("[ToolCard] _rebuild: controller=None!")

        # 确保所有工具都在 toggles 中
        all_tools = get_all_tools()
        defaults = get_default_toggles(all_tools)
        for name in all_tools:
            if name not in toggles:
                toggles[name] = defaults[name]

        # 按组构建（registry 动态分组，组内危险工具在前）
        groups = self._get_groups()
        for group_name, tool_names in groups:
            self._build_group(group_name, tool_names, toggles)

        self._layout.addStretch()

        # [PROBE] 重建完成：顶层窗口快照 + 耗时（性能基线）
        _probe_visible_top_windows("rebuild_end")
        logger.info(f"[ToolCard] _rebuild done: {(_time.monotonic() - t0) * 1000:.0f} ms, groups={len(groups)}")

    def _get_groups(self) -> list:
        """从 registry 动态聚合分组：[(group_name, [tool_name, ...]), ...]

        组排序：危险工具占比高/含危险工具的组在前，全安全组在后。
        ★ 性能：按 registry version 缓存，避免 _apply_toggles（每次状态变化
        调用）反复执行 group_map + 排序；registry 变更（热重载）时 version
        递增自动失效。
        """
        version = ToolRegistry.get_instance().version()
        if version != self._groups_cache_version:
            groups = ToolRegistry.get_instance().group_map()
            ordered = sorted(
                groups.items(),
                key=lambda kv: (
                    # 含危险工具 → 排前
                    0 if any(r.danger == DANGER_DANGEROUS for r in kv[1]) else 1,
                    # 组内危险工具数降序
                    -sum(1 for r in kv[1] if r.danger == DANGER_DANGEROUS),
                    kv[0],
                ),
            )
            self._groups_cache = [(g, [r.name for r in tools]) for g, tools in ordered]
            self._groups_cache_version = version
        return self._groups_cache

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

        # 更新单个工具关闭策略下拉(值 + 可见性:仅开关关闭时显示)
        # 与行渲染/右上角"未统一"判定共用 get_active_tool_behavior_map,口径一致
        policies_map = self._controller.get_active_tool_behavior_map()
        for tool_name, combo in self._policy_combos.items():
            enabled = toggles.get(tool_name, True)
            target_hidden = enabled  # 开关开启 → 策略下拉隐藏
            if combo.isHidden() != target_hidden:
                combo.setVisible(not enabled)
            policy = policies_map.get(tool_name, self._controller.get_behavior())
            idx = combo.findData(policy)
            if idx >= 0 and idx != combo.currentIndex():
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)

        # 更新整组开关 + 组标题"启用数/总数"
        for group_name, tool_names in self._get_groups():
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
        for group_name, tool_names in self._get_groups():
            gs = self._group_switches.get(group_name)
            if gs:
                all_on = all(toggles.get(t, True) for t in tool_names)
                gs.blockSignals(True)
                gs.setChecked(all_on)
                gs.blockSignals(False)

    def _build_group(self, group_name: str, tool_names: list, all_toggles: dict):
        """构建一个工具组（组内危险工具数驱动配色）"""
        Colors.refresh()
        # 组内是否含危险工具：含 → 红系主题；全安全 → 绿系主题
        has_danger = any(ToolRegistry.get_instance().get_danger(t) == DANGER_DANGEROUS for t in tool_names)
        border_color = "rgba(34,197,94,0.2)" if not has_danger else "rgba(255,80,80,0.2)"
        header_bg = "rgba(34,197,94,0.06)" if not has_danger else "rgba(255,80,80,0.08)"

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
        header.setStyleSheet(f"background: {header_bg}; border: none; border-radius: 8px;")
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
        group_switch.checkedChanged.connect(lambda checked, names=tool_names: self._on_group_toggled(names, checked))

        # 安全组默认折叠
        if not has_danger:
            body.setVisible(False)

    def _build_tool_row(self, tool_name: str, all_toggles: dict) -> QWidget:
        """构建单个工具行（中文名 + 来源标签 + 危险标记 + registry 描述）"""
        row = QWidget()
        row.setStyleSheet("background: transparent; border: none;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 3, 0, 3)
        row_layout.setSpacing(8)

        enabled = all_toggles.get(tool_name, True)

        # 工具元数据（registry 驱动：中文名 / 描述 / 危险级别）
        meta = ToolRegistry.get_instance().get_meta(tool_name)
        display_name = meta.get("cn_name") or tool_name
        desc = meta.get("description", "")
        is_danger = meta.get("danger") == DANGER_DANGEROUS
        source = meta.get("source", "builtin")
        # root_kind 经 plugin_tool_loader 写入 metadata（_plugin_root_kind），
        # builtin 工具走 trusted 种子路径，无 metadata 时按 builtin 兜底。
        reg = ToolRegistry.get_instance().get(tool_name)
        plugin_root_kind = (reg.metadata or {}).get("_plugin_root_kind", "") if reg is not None else ""

        name_label = QLabel(display_name)
        name_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none; "
            f"{font_size_css(12)} {get_font_family_css()}"
        )
        if is_danger:
            # 危险工具：名字加 🔥 标记 + 微红着色
            name_label.setText(f"🔥 {display_name}")
            name_label.setStyleSheet(
                f"color: #ff6b6b; background: transparent; border: none; {font_size_css(12)} {get_font_family_css()}"
            )
            name_label.setToolTip(f"{display_name}（危险操作：{desc or '可能修改系统状态'}）")
        else:
            name_label.setToolTip(f"{display_name}（安全操作）")

        # 来源标签（与 hook 配置卡片 sourceLabel 一致：彩色小色块 + 文字）
        # 布局顺序：来源 tag → 工具名 → 描述
        source_color, source_text = _format_source_label(source, plugin_root_kind)
        source_label = QLabel(source_text)
        source_label.setStyleSheet(
            f"background-color: {source_color}; color: white; "
            f"{font_size_css(10)} {get_font_family_css()} "
            f"font-weight: bold; padding: 1px 6px; border-radius: 4px;"
        )
        source_label.setFixedHeight(18)
        # tooltip 提示完整 plugin 名 + 描述
        if source.startswith("plugin:"):
            full_plugin = source[len("plugin:") :]
            source_label.setToolTip(f"来源：{plugin_root_kind or 'system'} 插件 {full_plugin}")
        else:
            source_label.setToolTip("来源：内置工具")
        row_layout.addWidget(source_label)
        row_layout.addSpacing(4)

        row_layout.addWidget(name_label)

        desc_label = _ElidedLabel(desc)
        desc_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        desc_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; background: transparent; border: none; "
            f"{font_size_css(10)} {get_font_family_css()}"
        )
        row_layout.addWidget(desc_label)

        # 关闭策略下拉(仅开关关闭时显示): ask=询问用户 / deny=直接拒绝
        # 策略取自 get_active_tool_behavior_map(与右上角"未统一"判定同源同口径)
        policy = self._controller.get_active_tool_behavior_map().get(tool_name, "deny") if self._controller else "deny"
        policy_combo = ComboBox()
        for value, label in OFF_BEHAVIOR_OPTIONS:
            policy_combo.addItem(label, userData=value)
        idx = policy_combo.findData(policy)
        if idx >= 0:
            policy_combo.setCurrentIndex(idx)
        policy_combo.setVisible(not enabled)  # 仅关闭时显示,开启时隐藏
        policy_combo.setFixedWidth(110)  # 固定宽度,防止撑宽行布局
        policy_combo.setToolTip("该工具关闭后：询问用户 / 直接拒绝")
        row_layout.addWidget(policy_combo)
        self._policy_combos[tool_name] = policy_combo
        policy_combo.currentIndexChanged.connect(lambda _idx, name=tool_name: self._on_tool_policy_changed(name))

        sw = SwitchButton()
        sw.setChecked(enabled)
        row_layout.addWidget(sw)
        self._toggle_widgets[tool_name] = sw

        sw.checkedChanged.connect(lambda checked, name=tool_name: self._on_tool_toggled(name, checked))

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
        logger.info(
            f"[ToolCard] _on_tool_toggled: {tool_name}={enabled}, agent_active={self._controller.is_agent_active()}"
        )
        # 直接更新 controller(触发信号链供外部使用)
        self._controller.set_user_toggle(tool_name, enabled)
        # 轻量级刷新 UI（controller 信号也会触发 _apply_toggles，但开销极低）
        self._apply_toggles()
        # 通知 frame 刷新统计
        self.togglesChanged.emit(self._controller.get_toggles())

    def _on_tool_policy_changed(self, tool_name: str):
        """用户编辑单工具关闭策略(ask/deny)
        - 非 agent 模式:user 和 active 同步更新(并持久化)
        - agent 模式:只更新 active(临时改 agent 生效权限,user 偏好不变)
        """
        from loguru import logger

        combo = self._policy_combos.get(tool_name)
        if combo is None or self._controller is None:
            return
        policy = combo.itemData(combo.currentIndex())
        if policy not in ("ask", "deny"):
            return
        logger.info(
            f"[ToolCard] _on_tool_policy_changed: {tool_name}={policy}, "
            f"agent_active={self._controller.is_agent_active()}"
        )
        self._controller.set_user_tool_policy(tool_name, policy)
        # 轻量级刷新 UI(controller 信号也会触发 _apply_toggles,开销极低)
        self._apply_toggles()

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
        隐藏期间收到 registry 变更(_needs_rebuild)时,显示时补一次重建。

        ★ 性能修复（2026-08-16）：若此刻已有排队的 registry 变更
        （_rebuild_pending），取消排队、吸收进本次重建——避免「打开卡片
        重建一次 → 紧随其后的变更再重建一次」的二次卡顿。
        """
        if self._rebuild_pending:
            # 有排队中的 registry 变更：取消，吸收进本次重建（打开后不立刻二次 rebuild）
            self._rebuild_pending = False
            self._queued_built = True
        if not self._built or self._needs_rebuild:
            self._needs_rebuild = False
            self._rebuild()
        else:
            self._apply_toggles()

    def hide_content(self):
        pass


class ToolControlCardFrame(SystemCardFrame):
    """工具控制卡片框架 — SystemCardFrame 包裹"""

    togglesChanged = pyqtSignal(dict)

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

        # ========== 右上角下拉框:关闭时行为(统一策略视图 + 强制统一入口) ==========
        self._behavior_combo = ComboBox(self)
        for value, label in OFF_BEHAVIOR_OPTIONS:
            self._behavior_combo.addItem(label, userData=value)
        # "未统一"仅作展示占位:各工具关闭策略不一致时显示,不可选中
        self._behavior_combo.addItem(MIXED_OPTION[1], userData=MIXED_OPTION[0])
        # 与 per-tool 策略对齐:一致显示该策略,不一致显示"未统一"
        self._sync_behavior_combo()
        self._behavior_combo.currentIndexChanged.connect(self._on_behavior_changed)

        # ========== 智能体徽章 + 恢复按钮(仅 agent 激活时显示) ==========
        self._active_agent_label = QLabel(self)
        self._active_agent_label.setStyleSheet(
            f"color: #ff9500; font-weight: 600; "
            f"background: rgba(255,149,0,0.12); border: 1px solid rgba(255,149,0,0.3); "
            f"border-radius: 6px; padding: 2px 8px; {font_size_css(12)} {get_font_family_css()}"
        )
        self._active_agent_label.setVisible(False)
        self._active_agent_label.setToolTip("当前工具权限由智能体命令注入,点击「恢复」可回到用户设置")

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
            self._controller.policiesChanged.connect(lambda _: self._sync_behavior_combo())
            # 初始状态
            self._on_agent_changed(self._controller.get_active_agent_name() or "")

    def set_controller(self, controller):
        """延迟绑定 controller(main_widget 在 super().__init__ 之后注入时使用)"""
        self._controller = controller
        if controller is None:
            return
        # 同步 behavior combo 当前值(与 per-tool 策略对齐)
        self._sync_behavior_combo()
        # 注入到 content
        self._card.set_controller(controller)
        # 监听 controller 信号
        controller.activeAgentChanged.connect(self._on_agent_changed)
        controller.policiesChanged.connect(lambda _: self._sync_behavior_combo())
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

    def _sync_behavior_combo(self):
        """右上角下拉与 per-tool 策略对齐：
        - 所有工具关闭策略一致 → 显示该策略
        - 无任何工具 → 显示全局 behavior
        - 不一致 → 显示"未统一"占位(不可选中)
        """
        if self._controller is None:
            return
        values = set(self._controller.get_active_tool_behavior_map().values())
        if len(values) == 1:
            target = next(iter(values))
        elif not values:
            target = self._controller.get_behavior()
        else:
            target = MIXED_OPTION[0]
        idx = self._behavior_combo.findData(target)
        if idx >= 0 and idx != self._behavior_combo.currentIndex():
            self._behavior_combo.blockSignals(True)
            self._behavior_combo.setCurrentIndex(idx)
            self._behavior_combo.blockSignals(False)

    def _on_behavior_changed(self, idx: int):
        value = self._behavior_combo.itemData(idx)
        if self._controller is None or value is None:
            return
        if value == MIXED_OPTION[0]:
            # "未统一"占位不可选中(理论不可达,防御性回退)
            self._sync_behavior_combo()
            return
        if value in ("deny", "ask"):
            # 强制统一:所有工具关闭策略设为该值(含开启的工具,保持判定一致)
            from app.tools.tool_classifier import get_all_tools

            all_tools = get_all_tools()
            self._controller.set_user_tool_policies({t: value for t in all_tools})
            # MINOR-1:同步全局 behavior——新注册工具(插件热插拔)缺失 per-tool 时回退一致
            # set_user_behavior 自带双态语义(agent 只改 active 不持久化 user)
            self._controller.set_user_behavior(value)
        self._sync_behavior_combo()

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
        # 刷新 behavior combo(与 per-tool 策略对齐)
        self._sync_behavior_combo()
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
