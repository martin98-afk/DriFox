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

from typing import Dict, List, Optional, Set

from loguru import logger
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    ExpandSettingCard,
    FluentIcon,
    PushButton,
    SearchLineEdit,
    SwitchButton,
    TransparentToolButton,
)

from app.plugins.component_items import ComponentItem, build_item_index, invalidate_item_index, supports_items
from app.plugins.kernel import COMPONENT_ORDER
from app.utils.design_tokens import Colors, SwitchStyles, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.elided_label import _ElidedLabel

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
# 搜索输入去抖
_SEARCH_DEBOUNCE_MS = 220
# 细项搜索的最小关键词长度（1 个字符命中过多，构建索引不划算）
_ITEM_SEARCH_MIN_LEN = 2


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
    """细项行 — 单个工具 / 单条 hook / 单个模板的开关"""

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
        self._name_label.setFixedWidth(150)
        layout.addWidget(self._name_label)

        self._desc_label = _ElidedLabel(item.description)
        self._desc_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        layout.addWidget(self._desc_label, 1)

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
        self._desc_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )


class ComponentRow(QWidget):
    """组件行 — 组件名 + 展开按钮（可细分的组件）+ 总开关 + 细项列表"""

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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── 头行 ──
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

        self._component_tag = _ElidedLabel(component)
        self._component_tag.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        header_layout.addWidget(self._component_tag, 1)

        # 展开按钮：细项是否真的存在要枚举后才知道，因此先无条件提供，
        # 展开后若无细项则显示一行提示（比预先枚举全部组件便宜得多）
        self._expand_btn = TransparentToolButton(FluentIcon.CHEVRON_RIGHT_MED, self)
        self._expand_btn.setFixedSize(22, 22)
        self._expand_btn.setCheckable(True)
        self._expand_btn.setToolTip("展开细项（可单独关闭其中某一项）")
        self._expand_btn.toggled.connect(self._on_expand_toggled)
        header_layout.addWidget(self._expand_btn)

        self.switch = SwitchButton()
        SwitchStyles.configure(self.switch)
        self.switch.setChecked(enabled)
        self.switch.checkedChanged.connect(self._on_switch_changed)
        header_layout.addWidget(self.switch)

        outer.addWidget(header)

        # ── 细项容器（默认隐藏，展开时才构建内容）──
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

    def _set_chevron(self, expanded: bool):
        self._expand_btn.setIcon(FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED)

    @property
    def is_expanded(self) -> bool:
        return self._expand_btn.isChecked()

    def load_items(self, items: List[ComponentItem], enabled_fn) -> None:
        """构建（或重建）细项行

        Args:
            items: 细项列表
            enabled_fn: (item_id) -> bool，取该条目当前是否启用
        """
        # 重建：清掉旧行（细项列表会随插件/工具注册变化，不复用）
        while self._items_layout.count():
            item = self._items_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._item_rows.clear()

        if not items:
            empty = QLabel("该组件没有可单独配置的细项")
            empty.setStyleSheet(
                _MUTED_LABEL_STYLE.format(
                    color=Colors.TEXT_MUTED,
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
            # 形参顺序必须匹配信号签名 (item_id, enabled)——按位置传参，
            # 把第一个写成 enabled 会让 bool 落进 str 槽位直接抛 TypeError
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
        """外部（如重载后）回写细项开关状态"""
        for item_id, row in self._item_rows.items():
            row.set_checked_silent(enabled_fn(item_id))

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
        self._component_tag.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        for row in self._item_rows.values():
            row.refresh_style()


class PluginSectionWidget(QWidget):
    """插件小节 — 插件名 + 来源标签 + 组件行列表"""

    component_toggled = pyqtSignal(str, str, bool)  # (plugin_name, component, enabled)
    item_toggled = pyqtSignal(str, str, str, bool)  # (plugin_name, component, item_id, enabled)
    items_expanding = pyqtSignal(str, str)  # (plugin_name, component) 首次展开，请求加载细项

    def __init__(self, plugin_name: str, description: str, is_system: bool, components: list, parent=None):
        super().__init__(parent)
        self._plugin_name = plugin_name

        section_layout = QVBoxLayout(self)
        section_layout.setContentsMargins(0, 2, 0, 2)
        section_layout.setSpacing(0)

        # ── 插件头行：色条 + 插件名 + 来源标签 + 描述 ──
        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 6, 8, 2)
        header_layout.setSpacing(8)

        kind_color = "#e74c3c" if is_system else "#2ecc71"
        anchor = QWidget(header)
        anchor.setFixedSize(3, 14)
        anchor.setStyleSheet(f"background: {kind_color}; border: none; border-radius: 1px;")
        header_layout.addWidget(anchor)

        name_label = QLabel(plugin_name)
        name_label.setStyleSheet(
            _TEXT_LABEL_STYLE.format(
                color=Colors.TEXT_PRIMARY,
                weight="font-weight: 600; ",
                font_size=font_size_css(12),
                font_family=get_font_family_css(),
            )
        )
        header_layout.addWidget(name_label)

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
        desc_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} font-size: {scale_font_size(11)}px;"
        )
        header_layout.addWidget(desc_label, 1)
        section_layout.addWidget(header)

        # ── 组件行 ──
        self._rows: Dict[str, ComponentRow] = {}
        for comp in sorted(components, key=_order_key):
            row = ComponentRow(comp, True, self)
            # 同 ItemRow：信号签名是 (component, enabled)，按位置传参
            row.toggled.connect(
                lambda component, enabled, name=self._plugin_name: self.component_toggled.emit(name, component, enabled)
            )
            row.item_toggled.connect(
                lambda component, item_id, enabled: self.item_toggled.emit(
                    self._plugin_name, component, item_id, enabled
                )
            )
            row.expand_requested_cb = lambda component=comp: self.items_expanding.emit(self._plugin_name, component)
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

    def load_component_items(self, component: str, items: List[ComponentItem], enabled_fn):
        row = self.component_row(component)
        if row is not None:
            row.load_items(items, enabled_fn)

    def reload_component_items(self, component: str, items: List[ComponentItem], enabled_fn):
        """重建细项行（组件整类开关后可用性会整体变化）

        只对**已展开**的组件做——未展开时构建几十个 ItemRow 纯属浪费。
        """
        row = self.component_row(component)
        if row is not None and row.is_expanded:
            row.reload_items(items, enabled_fn)
            row.sync_item_states(enabled_fn)

    def set_component_expanded(self, component: str, expanded: bool):
        row = self._rows.get(component)
        if row is not None:
            row.set_expanded(expanded)

    def apply_item_filter(self, component: str, keyword: str, hits: Optional[Set[str]]):
        row = self._rows.get(component)
        if row is not None:
            row.apply_item_filter(keyword, hits)

    def refresh_style(self):
        Colors.refresh()
        for row in self._rows.values():
            row.refresh_style()


class PluginComponentsCard(ExpandSettingCard):
    """插件组件开关卡 — 系统设置「插件启用」页，按插件分组控制子项启停"""

    def __init__(self, parent=None):
        super().__init__(
            get_icon("配置管理"),
            "插件组件",
            "控制插件内部子项（Hooks/LSP/工具等）的启停",
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
        self._item_off = 0
        # 复用的尾部控件（分页按钮 / 空态提示），避免每次重建堆积孤儿 widget
        self._more_btn: Optional[PushButton] = None
        self._empty_label: Optional[QLabel] = None

        # 组件行直接进 viewLayout：外层分页自身是滚动区，内嵌滚动区会导高度塌缩
        self.viewLayout.setSpacing(4)
        self._body_layout = self.viewLayout

        self._build_search_bar()
        self._rebuild()

    # ── 构建 ──

    def _build_search_bar(self):
        self._search_bar = SearchLineEdit(self)
        self._search_bar.setPlaceholderText("搜索插件名 / 组件名 / 细项（工具名、hook id…）")
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
        """[(name, is_system, description, [components])]，system 优先、同名按字母序"""
        pm = self._pm()
        plugins = sorted(pm.get_enabled_plugins(), key=lambda p: (not p.is_system, p.name.lower()))
        entries = []
        for plugin in plugins:
            comps = [c for c, v in plugin.components.items() if v]
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
        self._sig = self._signature()
        self._entries = self._collect_entries()
        self._visible, self._item_hits = self._match(self._keyword)
        self._recompute_counts()
        self._mount()

    def _mount(self):
        """按当前过滤结果与分页上限挂载小节（不销毁任何已建小节）"""
        # takeAt 只解绑、不删除，小节留在 _pool 里复用
        while self._body_layout.count():
            self._body_layout.takeAt(0)
        # 脱离布局的子 widget 不会自动隐藏，会浮到卡片左上角——必须显式 hide
        for section in self._pool.values():
            section.hide()
        self._body_layout.addWidget(self._search_bar)

        pm = self._pm()
        for entry, matched in self._visible[: self._page_limit]:
            name = entry[0]
            section = self._pool.get(name)
            if section is None:
                section = self._create_section(entry)
                self._pool[name] = section
            self._apply_state(section, entry, matched, pm)
            self._body_layout.addWidget(section)
            section.show()

        self._mount_more_button(len(self._visible) - self._page_limit)
        self._mount_empty_state(not self._visible)
        self._refresh_summary_text()

    def _create_section(self, entry: tuple) -> PluginSectionWidget:
        name, is_system, description, comps = entry
        section = PluginSectionWidget(name, description, is_system, comps, self)
        section.component_toggled.connect(self._on_component_toggled)
        section.item_toggled.connect(self._on_item_toggled)
        section.items_expanding.connect(self._on_items_expanding)
        return section

    def _apply_state(self, section: PluginSectionWidget, entry: tuple, matched: Optional[List[str]], pm):
        """回写开关状态、按匹配结果过滤、处理搜索命中的自动展开"""
        name, _is_system, _desc, comps = entry
        keyword = self._keyword.lower()
        hits = self._item_hits.get(name, {})

        for comp in comps:
            visible = matched is None or comp in matched
            # 搜索会把插件内部的组件行一并过滤（只留命中的那些）
            row = section.component_row(comp)
            if row is not None:
                row.setVisible(visible)
                if visible:
                    row.set_checked_silent(pm.is_component_enabled(name, comp))

            if not visible:
                continue
            # 搜索命中细项 → 自动展开并只显示命中的条目
            if comp in hits:
                self._ensure_items_loaded(section, name, comp, pm)
                section.set_component_expanded(comp, True)
                section.apply_item_filter(comp, keyword, hits[comp])
            elif keyword:
                section.set_component_expanded(comp, False)

    def _ensure_items_loaded(self, section: PluginSectionWidget, plugin_name: str, component: str, pm):
        from app.plugins.component_items import list_component_items

        items = list_component_items(plugin_name, component)
        section.load_component_items(
            component, items, lambda item_id: pm.is_item_enabled(plugin_name, component, item_id)
        )

    def _on_items_expanding(self, plugin_name: str, component: str):
        """组件行首次展开：现枚举细项并构建行"""
        section = self._pool.get(plugin_name)
        if section is None:
            return
        pm = self._pm()
        self._ensure_items_loaded(section, plugin_name, component, pm)
        # 若当前有搜索词，展开后立即应用过滤
        if self._keyword:
            hits = self._item_hits.get(plugin_name, {}).get(component)
            section.apply_item_filter(component, self._keyword.lower(), hits)

    def _mount_more_button(self, remaining: int):
        """「显示更多」按钮复用同一个实例（每次新建会在卡片上堆积孤儿控件）"""
        if remaining <= 0:
            if self._more_btn is not None:
                self._more_btn.hide()
            return
        if self._more_btn is None:
            self._more_btn = PushButton(self)
            self._more_btn.clicked.connect(self._on_show_more)
        self._more_btn.setText(f"显示更多（还有 {remaining} 个插件）")
        self._more_btn.show()
        self._body_layout.addWidget(self._more_btn)

    def _mount_empty_state(self, show: bool):
        if show:
            if self._empty_label is None:
                self._empty_label = QLabel(self)
                self._empty_label.setAlignment(Qt.AlignCenter)
            self._empty_label.setText("没有匹配的插件或组件")
            self._empty_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; background: transparent; "
                f"{get_font_family_css()} font-size: {scale_font_size(12)}px;"
            )
            self._empty_label.show()
            self._body_layout.addWidget(self._empty_label)
        elif self._empty_label is not None:
            self._empty_label.hide()

    def _on_show_more(self):
        self._page_limit += _PAGE_STEP
        self._mount()

    # ── 统计 ──

    def _recompute_counts(self):
        """重算摘要计数（仅在重建时执行，toggle 走增量）"""
        try:
            pm = self._pm()
            keys = pm.disabled_keys()
            total = 0
            component_off = 0
            item_off = 0
            for name, _is_system, _desc, comps in self._entries:
                for comp in comps:
                    total += 1
                    if f"{name}:{comp}" in keys:
                        component_off += 1
            for key in keys:
                parts = key.split(":", 2)
                if len(parts) == 3 and parts[2]:
                    item_off += 1
            self._total = total
            self._component_off = component_off
            self._item_off = item_off
        except Exception as e:
            logger.warning(f"[PluginComponentsCard] 统计组件数量失败: {e}")

    def _refresh_summary_text(self):
        card = self.card
        if not hasattr(card, "contentLabel"):
            return
        if not self._total:
            card.contentLabel.setText("暂无组件")
            return
        text = f"共 {self._total} 个组件 · 已停用 {self._component_off} 个"
        if self._item_off:
            text += f" · 细项停用 {self._item_off} 个"
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
            self._component_off += -1 if enabled else 1
            self._sig = self._signature()
            self._refresh_summary_text()
            # 组件整类开关会改变该组件下细项的可用性，已展开的要重建
            section = self._pool.get(plugin_name)
            if section is not None:
                from app.plugins.component_items import list_component_items

                items = list_component_items(plugin_name, component)
                section.reload_component_items(
                    component, items, lambda item_id: pm.is_item_enabled(plugin_name, component, item_id)
                )
            self._defer_hot_reload(
                PluginHostService.get_instance().on_plugin_component_toggled, plugin_name, component, enabled
            )
        except Exception as e:
            logger.error(f"[PluginComponentsCard] 切换 {plugin_name}:{component} 失败: {e}")

    def _on_item_toggled(self, plugin_name: str, component: str, item_id: str, enabled: bool):
        try:
            from app.core.plugin_host_service import PluginHostService

            pm = self._pm()
            if pm.is_item_enabled(plugin_name, component, item_id) == enabled:
                return  # 幂等
            pm.set_item_enabled(plugin_name, component, item_id, enabled)
            self._item_off += -1 if enabled else 1
            self._sig = self._signature()
            self._refresh_summary_text()
            self._defer_hot_reload(
                PluginHostService.get_instance().on_plugin_item_toggled, plugin_name, component, item_id, enabled
            )
        except Exception as e:
            logger.error(f"[PluginComponentsCard] 切换细项 {plugin_name}:{component}:{item_id} 失败: {e}")

    def _defer_hot_reload(self, fn, *args):
        """把热重载挪到下一轮事件循环

        重载涉及写盘、模块导入与 registry 重建，同步做会卡住 SwitchButton
        的动画；延后一轮让开关先渲染出按下状态，再显示等待光标。
        """
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
            invalidate_item_index()

    # ── 外部刷新 ──

    def refresh_components(self, force: bool = False):
        """重建插件小节（设置卡打开 / 插件热重载后调用）

        Args:
            force: True 时忽略脏检查强制重建，并丢弃细项索引缓存
        """
        if force:
            invalidate_item_index()
        elif self._sig is not None and self._sig == self._signature():
            return  # 插件清单与禁用集都没变，无需重建
        self._rebuild()

    def refresh_style(self):
        """主题变更刷新"""
        for section in self._pool.values():
            section.refresh_style()
