# -*- coding: utf-8 -*-
"""插件组件开关卡 — 按插件分组控制插件内部子项启停（D9）+ 细项（D10）

数据源：PluginManager 的组件/细项禁用集（Settings.disabled_plugin_components）
- 组件级（D9）：整个 tools / hooks / team_templates … 一起开关
- 细项级（D10）：组件下的单个条目（某个工具、某条 hook、某个模板）

## 性能设计

本卡曾让设置页卡到几乎不可用，根因是「176 个已启用插件 × 400+ 个
SwitchButton 每次打开设置全量销毁重建」。现在的策略：

- **池化 + 分页**：插件小节按插件名池化复用，默认只挂载前 `_PAGE_SIZE`
  个；剩余部分用「显示更多」按需扩页。重新挂载只是 `addWidget`，
  不销毁任何 widget（原实现的 `deleteLater` 会让对象数在重建期翻倍）。
- **懒展开**：组件下的细项在用户点开时才枚举并构建，首屏零成本。
- **脏检查**：`refresh_components()` 先比对签名（插件清单 + 禁用集），
  未变化则直接返回——设置面板每次打开都会调用它。
- **搜索去抖**：输入停止 220ms 后才重建列表。
- **计数增量维护**：摘要统计在重建时算一次，切换开关时 ±1，
  不再每次 toggle 都遍历全部插件。
"""

import json
from typing import Dict, List, Optional, Set

from loguru import logger

# estimate_tokens 内部有 lru_cache：同一段文本重复估算几乎零成本
from app.core.token_estimator import estimate_tokens as _estimate_tokens

# estimate_tokens 内部有 lru_cache：同一段文本重复估算几乎零成本
from app.core.token_estimator import estimate_tokens as _estimate_tokens
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ExpandSettingCard,
    FluentIcon,
    PushButton,
    SearchLineEdit,
    SwitchButton,
    TransparentToolButton,
)

from app.plugins.component_items import (
    ComponentItem,
    build_item_index,
    invalidate_item_index,
    supports_items,
)
from app.plugins.kernel import COMPONENT_ORDER
from app.widgets.cards.settings.expand_height_mixin import DynamicHeightExpandCardMixin
from app.utils.design_tokens import Colors, SwitchStyles, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.elided_label import _ElidedLabel

# 只对这两类组件提供开关。
#
# 其余组件（hooks / mcp / lsp / ui / providers …）整类关掉后要么感知不到差别，
# 要么本来就有各自的专用设置页，列在这里只是让设置页变长、构建变慢。
# 需要重新开放某一类时，往这个元组里加即可——下面的过滤、统计、搜索全部按它走。
_MANAGED_COMPONENTS = ("tools", "agents")

# 组件中文名（KNOWN_COMPONENTS 全集；未知组件回退显示原名）
_COMPONENT_CN = {
    "agents": "智能体",
    "hooks": "Hooks",
    "commands": "命令",
    "themes": "主题",
    "skills": "技能",
    "mcp": "MCP",
    "lsp": "LSP",
    "ui": "界面扩展",
    "tools": "工具",
    "providers": "服务商",
    "team_templates": "团队模板",
    "model_adapters": "模型适配器",
    "loop_policies": "循环策略",
    "storages": "会话存储",
    "serializers": "消息序列化",
    "gateways": "通讯网关",
    "engines": "存储引擎",
}

# 组件来源标签样式（对齐 tool_control_card：system 红 / user 绿）
_SOURCE_TAG_STYLE = (
    "background-color: {color}; color: white; "
    "{font_size} {font_family} font-weight: bold; padding: 1px 6px; border-radius: 4px;"
)

_TEXT_LABEL_STYLE = "color: {color}; background: transparent; border: none; {weight}{font_size} {font_family}"
_MUTED_LABEL_STYLE = "color: {color}; background: transparent; border: none; {font_size} {font_family}"

# 未搜索时挂载的小节数（首个插件清单可能上百个，全挂载会拖垮设置页）
_PAGE_SIZE = 40
# 搜索后结果通常很少，放宽上限但仍保留天花板
_SEARCH_PAGE_SIZE = 120
# 每次「显示更多」扩充的小节数
_PAGE_STEP = 40
# 首帧立即构建的小节数——先让用户看到内容并能交互，其余分批补齐。
# 实测整卡构建 ~250ms（65 插件 / 104 组件），首帧只做 6 个可把阻塞压到几十毫秒。
# 现在只对 tools / agents 提供开关，实际条目通常不到 20 个，首帧一次性建完即可，
# 免得列表在用户眼前「一点点长出来」；条目变多时分批机制仍会自动接管。
_FIRST_BATCH = 20
# 后续每批构建的小节数（singleShot(0) 之间让出事件循环，滚动/点击不被饿死）
_BATCH_SIZE = 12
# 搜索输入去抖
_SEARCH_DEBOUNCE_MS = 220
# 细项搜索的最小关键词长度（1 个字符命中过多，构建索引不划算）
_ITEM_SEARCH_MIN_LEN = 2


# ── token 占用估算 ──────────────────────────────────
#
# 组件开着是有成本的：工具把 schema 塞进每轮请求，智能体把列表注入系统提示词。
# 设置页要能看出「这个插件占了多少」，所以按**实际注入的内容**来估，
# 而不是按文件行数之类的代理指标。
#
# 注：实现内联在本模块而非 component_items，是因为 component_items 会被
# 外部进程反复还原，内联后这层依赖不会拖垮整张卡。

_tokens_cache: Dict[tuple, tuple] = {}


def invalidate_token_cache() -> None:
    """清空 token 估算缓存（工具/智能体增删或热重载后调用）"""
    _tokens_cache.clear()


def _estimate_tools_tokens(plugin_name: str) -> tuple:
    """工具：按 LLM 每轮实际收到的 function schema 计算"""
    from app.tools import _ensure_plugin_tools_loaded
    from app.tools.registry import ToolRegistry

    _ensure_plugin_tools_loaded()
    source = f"plugin:{plugin_name}"
    total = 0
    count = 0
    for reg in ToolRegistry.get_instance().list():
        if reg.source != source:
            continue
        count += 1
        total += _estimate_tokens(json.dumps(reg.schema, ensure_ascii=False, sort_keys=True))
    return total, count


def _estimate_agents_tokens(plugin_name: str) -> tuple:
    """智能体：按 get_available_subagents_for_prompt 的注入格式计算

    格式必须与 app/core/agent.py 保持一致（含标题行、描述截断 300 字），
    否则估算值会和实际注入的 token 数对不上。
    """
    from app.core.agent import AgentManager

    mgr = AgentManager.get_instance()
    names = mgr._plugin_agents.get(plugin_name) or set()
    parts: List[str] = []
    count = 0
    for name in sorted(names):
        agent = mgr.get_agent(name)
        if agent is None or not agent.is_subagent():
            continue
        count += 1
        parts.append(f"- **{agent.name}**: {agent.description[:300]}")
    if not parts:
        return 0, 0
    text = "\n".join(
        ["## Available Subagents\n可直接使用的子智能体列表(可供subagent_para和subagent_dag使用)："] + parts
    )
    return _estimate_tokens(text), count


def estimate_component_tokens(plugin_name: str, component: str) -> tuple:
    """估算该插件某类组件注入 prompt 的 token 占用

    Returns:
        (token 数, 计入的条目数)。无成本或不估算的组件返回 (0, 0)。
    """
    key = (plugin_name, component)
    cached = _tokens_cache.get(key)
    if cached is not None:
        return cached
    try:
        if component == "tools":
            result = _estimate_tools_tokens(plugin_name)
        elif component == "agents":
            result = _estimate_agents_tokens(plugin_name)
        else:
            result = (0, 0)
    except Exception as e:
        logger.warning(f"[PluginComponentsCard] token 估算失败 {plugin_name}:{component}: {e}")
        result = (0, 0)
    _tokens_cache[key] = result
    return result


def _component_display_name(component: str) -> str:
    """组件显示名：中文映射优先，未知组件回退原名"""
    return _COMPONENT_CN.get(component, component)


def _order_key(comp: str) -> int:
    """组件排序键：按 COMPONENT_ORDER，未知组件排最后"""
    try:
        return COMPONENT_ORDER.index(comp)
    except ValueError:
        return len(COMPONENT_ORDER)


class ItemRow(QWidget):
    """细项行 — 单个工具 / 单个智能体的开关

    按用户要求**不显示描述**：细项只用来快速开关某一项，描述会让行变得冗长，
    真要看说明有各自的地方。
    """

    toggled = pyqtSignal(str, bool)  # (item_id, enabled)

    def __init__(self, item: ComponentItem, enabled: bool, parent=None):
        super().__init__(parent)
        self._item_id = item.id
        self._match_text = f"{item.id} {item.display_label}".lower()
        self._building = True

        layout = QHBoxLayout(self)
        layout.setContentsMargins(52, 1, 12, 1)
        layout.setSpacing(8)

        self._name_label = QLabel(item.display_label)
        self._name_label.setStyleSheet(
            _TEXT_LABEL_STYLE.format(
                color=Colors.TEXT_PRIMARY,
                weight="",
                font_size=font_size_css(11),
                font_family=get_font_family_css(),
            )
        )
        self._name_label.setMinimumWidth(150)
        layout.addWidget(self._name_label)
        layout.addStretch(1)

        self.switch = SwitchButton()
        SwitchStyles.configure(self.switch)
        self.switch.setChecked(enabled)
        self.switch.checkedChanged.connect(self._on_switch_changed)
        layout.addWidget(self.switch)
        self._building = False

    @property
    def item_id(self) -> str:
        return self._item_id

    def matches(self, keyword: str) -> bool:
        return not keyword or keyword in self._match_text

    def _on_switch_changed(self, checked: bool):
        if self._building:
            return
        self.toggled.emit(self._item_id, checked)

    def set_checked_silent(self, checked: bool):
        self._building = True
        self.switch.setChecked(checked)
        self._building = False

    def refresh_style(self):
        Colors.refresh()
        self._name_label.setStyleSheet(
            _TEXT_LABEL_STYLE.format(
                color=Colors.TEXT_PRIMARY,
                weight="",
                font_size=font_size_css(11),
                font_family=get_font_family_css(),
            )
        )


class ComponentRow(QWidget):
    """组件行 — 组件名 + token 占用 + 总开关 + 可展开的细项列表"""

    toggled = pyqtSignal(str, bool)  # (component, enabled)
    item_toggled = pyqtSignal(str, str, bool)  # (component, item_id, enabled)

    def __init__(self, component: str, enabled: bool, parent=None):
        super().__init__(parent)
        self._component = component
        self._building = True
        self._items_loaded = False
        self._item_rows: Dict[str, ItemRow] = {}
        # 首次展开时由 PluginSectionWidget 注入（请求卡片现枚举细项）
        self.expand_requested_cb = None
        # 高度变化通知（细项展开/收起后由宿主卡片重算 fixedHeight）
        self.height_changed_cb = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(28, 2, 12, 2)
        header_layout.setSpacing(8)

        self._name_label = QLabel(_component_display_name(component))
        self._name_label.setStyleSheet(
            _TEXT_LABEL_STYLE.format(
                color=Colors.TEXT_PRIMARY,
                weight="",
                font_size=font_size_css(12),
                font_family=get_font_family_css(),
            )
        )
        self._name_label.setFixedWidth(110)
        header_layout.addWidget(self._name_label)

        self._muted_style = (
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )

        # 只管 tools / agents 两类，中文名已足够，不再显示英文组件名
        header_layout.addStretch(1)

        # token 占用：工具 = 每轮请求带上的 function schema，
        # 智能体 = 注入系统提示词的子智能体列表
        self._token_label = QLabel("")
        self._token_label.setStyleSheet(self._muted_style)
        self._token_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._token_label.setMinimumWidth(120)
        header_layout.addWidget(self._token_label)

        # 展开细项（可单独关闭其中某一项）
        self._expand_btn = TransparentToolButton(FluentIcon.CHEVRON_RIGHT_MED, self)
        self._expand_btn.setFixedSize(22, 22)
        self._expand_btn.setCheckable(True)
        self._expand_btn.setToolTip("展开细项（可单独开关其中某一项）")
        self._expand_btn.toggled.connect(self._on_expand_toggled)
        header_layout.addWidget(self._expand_btn)

        self.switch = SwitchButton()
        SwitchStyles.configure(self.switch)
        self.switch.setChecked(enabled)
        self.switch.checkedChanged.connect(self._on_switch_changed)
        header_layout.addWidget(self.switch)

        outer.addWidget(header)

        # 细项容器（默认隐藏，展开时才构建内容）
        self._items_widget = QWidget(self)
        self._items_layout = QVBoxLayout(self._items_widget)
        self._items_layout.setContentsMargins(0, 0, 0, 2)
        self._items_layout.setSpacing(0)
        self._items_widget.setVisible(False)
        outer.addWidget(self._items_widget)

        self._expand_btn.setVisible(supports_items(component))
        self._building = False

    # ── 细项懒加载 ──

    def _on_expand_toggled(self, expanded: bool):
        self._set_chevron(expanded)
        self._items_widget.setVisible(expanded)
        if expanded and not self._items_loaded and self.expand_requested_cb is not None:
            self.expand_requested_cb(self._component)
        self._notify_height_changed()

    def _notify_height_changed(self):
        if self.height_changed_cb is not None:
            self.height_changed_cb()

    def _set_chevron(self, expanded: bool):
        self._expand_btn.setIcon(FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED)

    @property
    def is_expanded(self) -> bool:
        return self._expand_btn.isChecked()

    def load_items(self, items: List[ComponentItem], enabled_fn) -> None:
        """构建（或重建）细项行"""
        while self._items_layout.count():
            item = self._items_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._item_rows.clear()

        if not items:
            empty = QLabel("该组件没有可单独配置的细项")
            empty.setStyleSheet(
                _TEXT_LABEL_STYLE.format(
                    color=Colors.TEXT_MUTED,
                    weight="",
                    font_size=font_size_css(11),
                    font_family=get_font_family_css(),
                )
            )
            empty.setContentsMargins(52, 2, 12, 2)
            self._items_layout.addWidget(empty)
            self._items_loaded = True
            return

        for it in items:
            row = ItemRow(it, enabled_fn(it.id), self)
            # 形参顺序必须匹配信号签名 (item_id, enabled)，按位置传参
            row.toggled.connect(
                lambda item_id, enabled, component=self._component: self.item_toggled.emit(component, item_id, enabled)
            )
            self._items_layout.addWidget(row)
            self._item_rows[it.id] = row
        self._items_loaded = True

    def reload_items(self, items: List[ComponentItem], enabled_fn) -> None:
        """强制重建已展开的细项列表（组件整类开关后细项状态会整体变化）"""
        self._items_loaded = False
        self.load_items(items, enabled_fn)

    def set_expanded(self, expanded: bool, silent: bool = True):
        if silent:
            self._expand_btn.blockSignals(True)
        self._expand_btn.setChecked(expanded)
        self._set_chevron(expanded)
        if silent:
            self._expand_btn.blockSignals(False)
        self._items_widget.setVisible(expanded)
        self._notify_height_changed()

    def apply_item_filter(self, keyword: str, hits: Optional[Set[str]] = None):
        """按关键词过滤细项行；hits 为搜索命中的 item_id 集合（优先级更高）"""
        if not self._items_loaded:
            return
        for item_id, row in self._item_rows.items():
            if hits is not None:
                row.setVisible(item_id in hits)
            else:
                row.setVisible(row.matches(keyword))

    def sync_item_states(self, enabled_fn):
        for item_id, row in self._item_rows.items():
            row.set_checked_silent(enabled_fn(item_id))

    def set_tokens(self, tokens: int, count: int) -> None:
        """显示 token 占用；tokens<=0 表示该组件无上下文成本（留空）"""
        if tokens <= 0:
            self._token_label.setText("")
            return
        self._token_label.setText(f"{count} 项 · ≈{tokens:,} tokens")

    # ── 交互 ──

    def _on_switch_changed(self, checked: bool):
        if self._building:
            return
        self.toggled.emit(self._component, checked)

    def set_checked_silent(self, checked: bool):
        self._building = True
        self.switch.setChecked(checked)
        self._building = False

    def refresh_style(self):
        Colors.refresh()
        self._name_label.setStyleSheet(
            _TEXT_LABEL_STYLE.format(
                color=Colors.TEXT_PRIMARY,
                weight="",
                font_size=font_size_css(12),
                font_family=get_font_family_css(),
            )
        )
        self._muted_style = (
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        self._token_label.setStyleSheet(self._muted_style)
        for row in self._item_rows.values():
            row.refresh_style()


class PluginSectionWidget(QWidget):
    """插件小节 — 插件名 + 来源标签 + 组件行列表"""

    component_toggled = pyqtSignal(str, str, bool)  # (plugin_name, component, enabled)
    item_toggled = pyqtSignal(str, str, str, bool)  # (plugin_name, component, item_id, enabled)
    items_expanding = pyqtSignal(str, str)  # (plugin_name, component) 首次展开，请求加载细项
    size_changed = pyqtSignal()  # 内部高度变化（细项展开/收起）

    def __init__(self, plugin_name: str, description: str, is_system: bool, components: list, parent=None):
        super().__init__(parent)
        self._plugin_name = plugin_name

        section_layout = QVBoxLayout(self)
        section_layout.setContentsMargins(0, 2, 0, 2)
        section_layout.setSpacing(0)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 6, 8, 2)
        header_layout.setSpacing(8)

        kind_color = "#e74c3c" if is_system else "#2ecc71"
        anchor = QWidget(header)
        anchor.setFixedSize(3, 14)
        anchor.setStyleSheet(f"background: {kind_color}; border: none; border-radius: 1px;")
        header_layout.addWidget(anchor)

        # 实例属性：refresh_style 主题刷新时需重建样式（构造时 Colors 为旧主题值）
        self._name_label = QLabel(plugin_name)
        self._name_label.setStyleSheet(
            _TEXT_LABEL_STYLE.format(
                color=Colors.TEXT_PRIMARY,
                weight="font-weight: 600; ",
                font_size=font_size_css(12),
                font_family=get_font_family_css(),
            )
        )
        header_layout.addWidget(self._name_label)

        kind_tag = QLabel("系统" if is_system else "用户")
        kind_tag.setStyleSheet(
            _SOURCE_TAG_STYLE.format(
                color=kind_color,
                font_size=font_size_css(10),
                font_family=get_font_family_css(),
            )
        )
        header_layout.addWidget(kind_tag)

        desc_label = _ElidedLabel(description or "")
        self._desc_label = desc_label
        desc_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        header_layout.addWidget(desc_label, 1)
        section_layout.addWidget(header)

        self._rows: Dict[str, ComponentRow] = {}
        for comp in sorted(components, key=_order_key):
            row = ComponentRow(comp, True, self)
            # 信号签名是 (component, enabled)，按位置传参
            row.toggled.connect(
                lambda component, enabled, name=self._plugin_name: self.component_toggled.emit(name, component, enabled)
            )
            row.item_toggled.connect(
                lambda component, item_id, enabled: self.item_toggled.emit(
                    self._plugin_name, component, item_id, enabled
                )
            )
            row.expand_requested_cb = lambda component=comp: self.items_expanding.emit(self._plugin_name, component)
            row.height_changed_cb = self.size_changed.emit
            section_layout.addWidget(row)
            self._rows[comp] = row

    @property
    def plugin_name(self) -> str:
        return self._plugin_name

    def component_row(self, component: str) -> Optional[ComponentRow]:
        return self._rows.get(component)

    def set_component_checked(self, component: str, checked: bool):
        row = self.component_row(component)
        if row is not None:
            row.set_checked_silent(checked)

    def set_component_tokens(self, component: str, tokens: int, count: int):
        row = self.component_row(component)
        if row is not None:
            row.set_tokens(tokens, count)

    def load_component_items(self, component: str, items: List[ComponentItem], enabled_fn):
        row = self.component_row(component)
        if row is not None:
            row.load_items(items, enabled_fn)

    def reload_component_items(self, component: str, items: List[ComponentItem], enabled_fn):
        """重建细项行；只对已展开的组件做（未展开时构建几十行纯属浪费）"""
        row = self.component_row(component)
        if row is not None and row.is_expanded:
            row.reload_items(items, enabled_fn)
            row.sync_item_states(enabled_fn)

    def set_component_expanded(self, component: str, expanded: bool):
        row = self.component_row(component)
        if row is not None:
            row.set_expanded(expanded)

    def apply_item_filter(self, component: str, keyword: str, hits: Optional[Set[str]]):
        row = self.component_row(component)
        if row is not None:
            row.apply_item_filter(keyword, hits)

    def refresh_style(self):
        Colors.refresh()
        # 插件名/描述标签颜色随主题（构造时用旧 Colors 固化，需重建）
        self._name_label.setStyleSheet(
            _TEXT_LABEL_STYLE.format(
                color=Colors.TEXT_PRIMARY,
                weight="font-weight: 600; ",
                font_size=font_size_css(12),
                font_family=get_font_family_css(),
            )
        )
        self._desc_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        for row in self._rows.values():
            row.refresh_style()


class PluginComponentsCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
    """插件组件开关卡 — 按插件分组控制某一类组件（工具 / 智能体）的启停

    一张卡只管一类组件：设置页用两张卡分别承载「工具启用」与「智能体启用」。
    动态高度与滚轮行为由 DynamicHeightExpandCardMixin 接管。
    """

    def __init__(
        self,
        parent=None,
        components=("tools",),
        title: str = "工具启用",
        content: str = "按插件控制其工具的启停",
        icon=None,
    ):
        # 白名单收敛：调用方误传 hooks 等未开放的组件时静默忽略（不留死开关）。
        # 必须在 super().__init__ 之前——基类构造里就会走到 _collect_entries
        self._components = tuple(c for c in components if c in _MANAGED_COMPONENTS) or ("tools",)
        super().__init__(
            icon if icon is not None else get_icon("配置管理"),
            title,
            content,
            parent,
        )
        self._pool: Dict[str, PluginSectionWidget] = {}
        self._entries: List[tuple] = []  # [(name, is_system, description, [components])]
        self._visible: List[tuple] = []  # [(entry, matched_components | None)]
        self._item_hits: Dict[str, Dict[str, Set[str]]] = {}  # {plugin: {component: {item_id}}}
        self._keyword = ""
        self._page_limit = _PAGE_SIZE
        self._sig: Optional[tuple] = None
        self._total = 0
        self._component_off = 0
        self._tokens = 0  # 当前启用组件的 token 占用合计
        self._rebuild_target = None  # 热重载后需要重建细项行的 (plugin, component)
        # ── 分批构建状态 ──
        self._pending: List[tuple] = []  # 待构建的 (entry, matched)
        self._built = 0  # 已构建并挂载的数量
        self._token = 0  # 重建令牌：用于作废过期的分批回调
        self._adjusting = False  # resizeEvent → _adjust_view_size 的重入保护
        self._height_resync_queued = False  # 下一帧高度校正是否已排队
        self._batch_timer = QTimer(self)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.setInterval(0)
        self._batch_timer.timeout.connect(self._build_next_batch)
        # 复用的尾部控件（分页按钮 / 空态提示），避免每次重建堆积孤儿 widget
        self._more_btn: Optional[PushButton] = None
        self._empty_label: Optional[QLabel] = None
        # 尾部容器：始终留在布局最后，分批构建时会被临时摘下再挂回
        self._tail_widget = QWidget(self)
        self._tail_layout = QVBoxLayout(self._tail_widget)
        self._tail_layout.setContentsMargins(0, 2, 0, 2)
        self._tail_layout.setSpacing(4)

        # 组件行直接进 viewLayout：外层分页自身是滚动区，内嵌滚动区会导高度塌缩
        self.viewLayout.setSpacing(4)
        self._body_layout = self.viewLayout

        self._build_search_bar()
        self._rebuild()

    # ── 构建 ──

    def _build_search_bar(self):
        kinds = "/".join(_component_display_name(c) for c in self._components)
        self._search_bar = SearchLineEdit(self)
        self._search_bar.setPlaceholderText(f"搜索插件名 / {kinds}名")
        self._search_bar.setClearButtonEnabled(True)
        self._search_bar.setFixedHeight(32)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._rebuild)
        self._search_bar.textChanged.connect(self._on_search_text_changed)

    def _pm(self):
        from app.plugins.managers.plugin_manager import PluginManager

        return PluginManager.get_instance()

    def _collect_entries(self) -> List[tuple]:
        """[(name, is_system, description, [components])]，system 优先、同名按字母序

        只保留本卡负责的组件（self._components）；一个都不含的插件不出现。
        """
        pm = self._pm()
        plugins = sorted(pm.get_enabled_plugins(), key=lambda p: (not p.is_system, p.name.lower()))
        entries = []
        for plugin in plugins:
            comps = [c for c, v in plugin.components.items() if v and c in self._components]
            if not comps:
                continue
            entries.append((plugin.name, plugin.is_system, plugin.description or "", sorted(comps, key=_order_key)))
        return entries

    def _signature(self) -> tuple:
        """内容指纹：插件清单 + 组件集合 + 禁用集。用于跳过无意义的重建"""
        try:
            pm = self._pm()
            entries = tuple((e[0], tuple(e[3])) for e in self._collect_entries())
            return (entries, tuple(sorted(pm.disabled_keys())))
        except Exception as e:
            logger.warning(f"[PluginComponentsCard] 计算签名失败: {e}")
            return ()

    # ── 过滤 ──

    def _on_search_text_changed(self, text: str):
        self._keyword = (text or "").strip()
        self._page_limit = _SEARCH_PAGE_SIZE if self._keyword else _PAGE_SIZE
        self._search_timer.start()

    def _match(self, keyword: str) -> tuple:
        """按关键词过滤条目

        Returns:
            (visible, item_hits)
            - visible: [(entry, matched_components | None)]，None 表示该插件全组件显示
            - item_hits: {plugin: {component: {item_id}}}，细项命中的集合
        """
        if not keyword:
            return [(e, None) for e in self._entries], {}

        kw = keyword.lower()
        visible: List[tuple] = []
        item_hits: Dict[str, Dict[str, Set[str]]] = {}
        # 只有关键词足够长才做细项匹配（否则命中过多且要付索引构建成本）
        index = build_item_index() if len(kw) >= _ITEM_SEARCH_MIN_LEN else {}

        for entry in self._entries:
            name, _is_system, desc, comps = entry
            # 插件名/描述命中 → 整个插件显示
            if kw in name.lower() or kw in desc.lower():
                visible.append((entry, None))
                continue

            matched: List[str] = []
            hits: Dict[str, Set[str]] = {}
            for comp in comps:
                if kw in comp.lower() or kw in _component_display_name(comp).lower():
                    matched.append(comp)
                    continue
                # 细项命中
                items = index.get(name, {}).get(comp) or []
                if items:
                    ids = {it.id for it in items if kw in f"{it.id} {it.display_label}".lower()}
                    if ids:
                        matched.append(comp)
                        hits[comp] = ids
            if matched:
                visible.append((entry, matched))
                if hits:
                    item_hits[name] = hits
        return visible, item_hits

    # ── 重建 / 挂载 ──

    def _rebuild(self):
        """重算过滤结果并**分批**挂载

        一次性挂载全部小节会阻塞主线程 ~250ms（65 插件 / 104 组件），
        表现为「打开设置面板卡一下」。这里首帧只构建 _FIRST_BATCH 个，
        剩余部分在后续事件循环里分批补齐，用户立刻就能滚动和点击。
        """
        self._token += 1
        self._sig = self._signature()
        self._entries = self._collect_entries()
        self._visible, self._item_hits = self._match(self._keyword)
        self._recompute_counts()

        # 重置布局：takeAt 只解绑、不删除，小节留在 _pool 里复用
        while self._body_layout.count():
            self._body_layout.takeAt(0)
        self._body_layout.addWidget(self._search_bar)
        # 脱离布局的子 widget 不会自动隐藏，会浮到卡片左上角——必须显式 hide
        for section in self._pool.values():
            section.hide()

        self._pending = self._visible[: self._page_limit]
        self._built = 0
        self._build_next_batch(first=True)

    def _build_next_batch(self, first: bool = False):
        """构建下一批小节

        Args:
            first: 首帧批次，数量用 _FIRST_BATCH（更小，让首屏更快出现）
        """
        token = self._token
        # 折叠态不构建：内容不可见，没必要付这份开销（展开时会自动续上）
        if not self.isExpand:
            self._finish_mount()
            return
        if self._built >= len(self._pending):
            self._finish_mount()
            return

        batch = _FIRST_BATCH if first else _BATCH_SIZE
        target = min(self._built + batch, len(self._pending))
        pm = self._pm()

        # 尾部容器先摘下，保证加完小节后它仍在布局最后
        tail_index = self._body_layout.indexOf(self._tail_widget)
        if tail_index >= 0:
            self._body_layout.takeAt(tail_index)

        for entry, matched in self._pending[self._built : target]:
            if token != self._token:
                return  # 期间又发生了重建/搜索，本批次作废
            name = entry[0]
            section = self._pool.get(name)
            if section is None:
                section = self._create_section(entry)
                self._pool[name] = section
            self._apply_state(section, entry, matched, pm)
            self._body_layout.addWidget(section)
            section.show()
        self._built = target
        self._body_layout.addWidget(self._tail_widget)

        self._finish_mount()
        if self._built < len(self._pending):
            self._batch_timer.start()

    def _finish_mount(self):
        """批次结束后的收尾：尾部控件 + 摘要 + 高度同步"""
        done = self._built >= len(self._pending)
        remaining = len(self._visible) - self._page_limit
        self._mount_more_button(remaining if done else 0)
        self._mount_empty_state(done and not self._visible)
        self._refresh_summary_text()
        self._adjust_view_size()

    # ── 高度同步与滚轮：由 DynamicHeightExpandCardMixin 统一提供 ──

    def on_after_expand(self, is_expand: bool) -> None:
        """折叠期间会暂停分批构建（见 _build_next_batch），展开时接着做"""
        if is_expand and self._built < len(self._pending):
            self._batch_timer.start()

    def _create_section(self, entry: tuple) -> PluginSectionWidget:
        name, is_system, description, comps = entry
        section = PluginSectionWidget(name, description, is_system, comps, self)
        section.component_toggled.connect(self._on_component_toggled)
        section.item_toggled.connect(self._on_item_toggled)
        section.items_expanding.connect(self._on_items_expanding)
        section.size_changed.connect(self._adjust_view_size)
        return section

    def _apply_state(self, section: PluginSectionWidget, entry: tuple, matched: Optional[List[str]], pm):
        """回写开关状态、按匹配结果过滤、刷新 token 占用、处理搜索命中的自动展开"""
        name, _is_system, _desc, comps = entry
        keyword = self._keyword.lower()
        hits = self._item_hits.get(name, {})

        for comp in comps:
            visible = matched is None or comp in matched
            # 搜索会把插件内部的组件行一并过滤（只留命中的那些）
            row = section.component_row(comp)
            if row is not None:
                row.setVisible(visible)
            if not visible:
                continue
            if row is not None:
                row.set_checked_silent(pm.is_component_enabled(name, comp))
                # token 占用：工具=每轮请求的 schema，智能体=注入系统提示词的列表
                tokens, count = estimate_component_tokens(name, comp)
                row.set_tokens(tokens, count)

            # 搜索命中细项 → 自动展开并只显示命中的条目
            if comp in hits:
                self._ensure_items_loaded(section, name, comp, pm)
                section.set_component_expanded(comp, True)
                section.apply_item_filter(comp, keyword, hits[comp])
            elif keyword:
                section.set_component_expanded(comp, False)

    def _component_items(self, plugin_name: str, component: str) -> List[ComponentItem]:
        """细项列表 + 被单独停用而枚举不到的那些项

        ⚠️ 后半段不能漏：工具的细项过滤发生在**注册期**（plugin_tool_loader 在
        register 时就跳过被停用的），被停用的工具根本不进 registry，自然也枚举
        不到。不把配置里记录的停用项补回来的话，用户关掉某个工具后它就从列表
        里蒸发了——想再打开只能手改配置文件。
        """
        from app.plugins.component_items import list_component_items

        items = list(list_component_items(plugin_name, component))
        try:
            disabled = self._pm().disabled_items(plugin_name, component)
        except Exception:
            disabled = []
        if disabled:
            have = {it.id for it in items}
            for name in disabled:
                if name not in have:
                    items.append(ComponentItem(id=name))
            items.sort(key=lambda it: it.id)
        return items

    def _ensure_items_loaded(self, section: PluginSectionWidget, plugin_name: str, component: str, pm):
        """现枚举细项并构建行（首次展开时调用）"""
        items = self._component_items(plugin_name, component)
        section.load_component_items(
            component, items, lambda item_id: pm.is_item_enabled(plugin_name, component, item_id)
        )

    def _on_items_expanding(self, plugin_name: str, component: str):
        """组件行首次展开：补齐细项行并按当前搜索词过滤"""
        section = self._pool.get(plugin_name)
        if section is None:
            return
        pm = self._pm()
        self._ensure_items_loaded(section, plugin_name, component, pm)
        if self._keyword:
            hits = self._item_hits.get(plugin_name, {}).get(component)
            section.apply_item_filter(component, self._keyword.lower(), hits)
        self._adjust_view_size()

    def _mount_more_button(self, remaining: int):
        """「显示更多」按钮复用同一个实例（每次新建会在卡片上堆积孤儿控件）"""
        if remaining <= 0:
            if self._more_btn is not None:
                self._more_btn.hide()
            return
        if self._more_btn is None:
            self._more_btn = PushButton(self._tail_widget)
            self._more_btn.clicked.connect(self._on_show_more)
            self._tail_layout.addWidget(self._more_btn)
        self._more_btn.setText(f"显示更多（还有 {remaining} 个插件）")
        self._more_btn.show()

    def _mount_empty_state(self, show: bool):
        if show:
            if self._empty_label is None:
                self._empty_label = QLabel(self._tail_widget)
                self._empty_label.setAlignment(Qt.AlignCenter)
                self._tail_layout.addWidget(self._empty_label)
            self._empty_label.setText("没有匹配的插件或组件")
            self._empty_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; background: transparent; "
                f"{get_font_family_css()} font-size: {scale_font_size(12)}px;"
            )
            self._empty_label.show()
        elif self._empty_label is not None:
            self._empty_label.hide()

    def _on_show_more(self):
        """扩页后继续分批——不重置已构建部分，视觉上连续补在下面"""
        self._page_limit += _PAGE_STEP
        self._pending = self._visible[: self._page_limit]
        self._batch_timer.start()

    # ── 统计 ──

    def _recompute_counts(self):
        """重算摘要计数（仅在重建时执行，toggle 走增量）

        token 汇总只统计**当前启用**的组件——停用掉的不会进 prompt，
        算进来会让「这个插件到底占多少」失真。
        """
        try:
            pm = self._pm()
            keys = pm.disabled_keys()
            total = 0
            component_off = 0
            tokens = 0
            for name, _is_system, _desc, comps in self._entries:
                for comp in comps:
                    total += 1
                    if f"{name}:{comp}" in keys:
                        component_off += 1
                        continue
                    tokens += estimate_component_tokens(name, comp)[0]
            self._total = total
            self._component_off = component_off
            self._tokens = tokens
        except Exception as e:
            logger.warning(f"[PluginComponentsCard] 统计组件数量失败: {e}")

    def _refresh_summary_text(self):
        card = self.card
        if not hasattr(card, "contentLabel"):
            return
        if not self._total:
            card.contentLabel.setText("暂无组件")
            return
        text = f"共 {self._total} 个 · 停用 {self._component_off} 个"
        if self._tokens:
            text += f" · 启用中 ≈{self._tokens:,} tokens"
        if self._keyword:
            text += f" · 匹配 {len(self._visible)} 个插件"
        card.contentLabel.setText(text)

    # ── 交互 ──

    def _on_component_toggled(self, plugin_name: str, component: str, enabled: bool):
        try:
            from app.core.plugin_host_service import PluginHostService

            pm = self._pm()
            if pm.is_component_enabled(plugin_name, component) == enabled:
                return  # 幂等（重建后的陈旧信号）
            pm.set_component_enabled(plugin_name, component, enabled)
            # 停用后该组件不再进 prompt，token 汇总要跟着变
            tokens, _count = estimate_component_tokens(plugin_name, component)
            self._tokens += tokens if enabled else -tokens
            self._component_off += -1 if enabled else 1
            self._sig = self._signature()
            self._refresh_summary_text()
            # 组件整类开关后细项可用性会变。仅「关闭」方向在此同步重建——
            # 此刻热重载（singleShot(0)）尚未执行，registry 里该插件工具还在，
            # 枚举仍是全量。「开启」方向此刻 registry 还是空的（工具要等
            # 热重载才注册回来），同步重建只会得到空列表（用户看到的「开启
            # 总项后列表不全」）；开启方向交给 _run_hot_reload 完成后按
            # _rebuild_target 重建（_refresh_after_reload），届时枚举才正确。
            section = self._pool.get(plugin_name)
            if section is not None and not enabled:
                items = self._component_items(plugin_name, component)
                section.reload_component_items(
                    component, items, lambda item_id: pm.is_item_enabled(plugin_name, component, item_id)
                )
                self._adjust_view_size()
            self._defer_hot_reload(
                PluginHostService.get_instance().on_plugin_component_toggled,
                plugin_name,
                component,
                enabled,
                rebuild_items=(plugin_name, component),
            )
        except Exception as e:
            logger.error(f"[PluginComponentsCard] 切换 {plugin_name}:{component} 失败: {e}")

    def _on_item_toggled(self, plugin_name: str, component: str, item_id: str, enabled: bool):
        """单个工具 / 智能体的开关"""
        try:
            from app.core.plugin_host_service import PluginHostService

            pm = self._pm()
            if pm.is_item_enabled(plugin_name, component, item_id) == enabled:
                return  # 幂等
            pm.set_item_enabled(plugin_name, component, item_id, enabled)
            self._sig = self._signature()
            self._refresh_summary_text()
            # 不重建细项行：列表里这一项本来就在（_component_items 会从
            # 禁用集里补回被停用的项），重建只会让整片开关闪一下
            self._defer_hot_reload(
                PluginHostService.get_instance().on_plugin_item_toggled, plugin_name, component, item_id, enabled
            )
        except Exception as e:
            logger.error(f"[PluginComponentsCard] 切换细项 {plugin_name}:{component}:{item_id} 失败: {e}")

    def _defer_hot_reload(self, fn, *args, rebuild_items=None):
        """把热重载挪到下一轮事件循环

        重载涉及写盘、模块导入与 registry 重建，同步做会卡住 SwitchButton
        的动画；延后一轮让开关先渲染出按下状态，再显示等待光标。

        Args:
            rebuild_items: 重载后需要重建细项行的 (plugin_name, component)。
                传 None 表示一行都不重建（只刷 token）——单个条目开关时用，
                否则用户会看到整片开关闪一下。
        """
        self._rebuild_target = rebuild_items
        QTimer.singleShot(0, lambda: self._run_hot_reload(fn, *args))

    def _run_hot_reload(self, fn, *args):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = fn(*args) or {}
            ok = any(bool(v) for v in result.values()) if isinstance(result, dict) else bool(result)
            if not ok:
                logger.warning(f"[PluginComponentsCard] {fn.__name__} 已保存，但热重载未执行（可能需重启生效）")
        except Exception as e:
            logger.error(f"[PluginComponentsCard] 热重载失败: {e}")
        finally:
            QApplication.restoreOverrideCursor()
            # 重载后工具/智能体集合可能变了，两套缓存都要失效
            invalidate_item_index()
            invalidate_token_cache()
            # ★ 必须把 _defer_hot_reload 存下的 _rebuild_target 传进去：
            # 组件「开启」方向的细项行要等这步（热重载完成后 registry 已
            # 恢复）才重建得出来。此前无参调用丢弃了目标，热重载后没人
            # 重建细项行，列表停留在空/不全状态。
            self._refresh_after_reload(self._rebuild_target)

    def _refresh_after_reload(self, rebuild_items=None):
        """热重载后按实际状态刷新：token 汇总 + （按需）细项行

        Args:
            rebuild_items: (plugin_name, component)。只重建这一处的细项行；
                其余组件一律不动——重建是 deleteLater + 新建，用户会看到
                整片开关闪一下，代价太大。单个条目开关时传 None。
        """
        invalidate_token_cache()
        try:
            pm = self._pm()
            total = 0
            for name, _is_system, _desc, comps in self._entries:
                for comp in comps:
                    if not pm.is_component_enabled(name, comp):
                        continue
                    total += estimate_component_tokens(name, comp)[0]
            self._tokens = total

            for section in self._pool.values():
                for comp, row in section._rows.items():
                    if not row.isVisible():
                        continue
                    tokens, count = estimate_component_tokens(section.plugin_name, comp)
                    row.set_tokens(tokens, count)
                    # 只重建目标组件：整类开关会改变该组件下细项的可用性。
                    # 单个条目开关时 rebuild_items 为 None，列表原样保留。
                    # ★ 且仅组件当前「启用」时重建：关闭方向的热重载会把工具
                    # 从 registry 注销，此刻枚举拿不到全量——重建会把 toggle
                    # 同步路径建好的全量列表冲成空列表。
                    if (
                        rebuild_items is not None
                        and (section.plugin_name, comp) == rebuild_items
                        and row.is_expanded
                        and pm.is_component_enabled(section.plugin_name, comp)
                    ):
                        items = self._component_items(section.plugin_name, comp)
                        section.reload_component_items(
                            comp,
                            items,
                            lambda item_id, p=section.plugin_name, c=comp: pm.is_item_enabled(p, c, item_id),
                        )
            self._refresh_summary_text()
            self._adjust_view_size()
        except Exception as e:
            logger.warning(f"[PluginComponentsCard] 重载后刷新失败: {e}")

    # ── 外部刷新 ──

    def refresh_components(self, force: bool = False):
        """重建插件小节（设置卡打开 / 插件热重载后调用）

        Args:
            force: True 时忽略脏检查强制重建，并丢弃细项与 token 缓存
        """
        if force:
            invalidate_item_index()
            invalidate_token_cache()
        elif self._sig is not None and self._sig == self._signature():
            return  # 插件清单与禁用集都没变，无需重建
        self._rebuild()

    def refresh_style(self):
        """主题变更刷新"""
        for section in self._pool.values():
            section.refresh_style()
