# -*- coding: utf-8 -*-
"""
模型选择卡片内容 - 底部卡片形式展示所有服务商的模型列表
"""

from typing import List, Optional, Tuple

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import IconWidget

from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget


def _format_cost_number(value) -> str:
    """格式化成本数值：数字用 :g 紧凑显示（3.0 → 3），非数字原样字符串。"""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _measure_name_width(names) -> int:
    """按 15px 字体（含 bold）测量模型名最大像素宽度，+12px 余量。

    用于服务商内对齐：模型名固定为组内最长名宽度，短名右侧留白，
    成本列从同一 x 位置开始，使同分组下各模型行纵向对齐比价。
    """
    probe = QLabel()
    probe.setStyleSheet(f"{get_font_family_css()} {font_size_css(15)};")
    probe.ensurePolished()
    fm = probe.fontMetrics()
    bold_font = probe.font()
    bold_font.setBold(True)
    fm_bold = QFontMetrics(bold_font)
    widths = [max(fm.horizontalAdvance(n), fm_bold.horizontalAdvance(n)) for n in names]
    return (max(widths) if widths else 0) + 12


# item 高度常量
_ITEM_HEIGHT = 34  # ModelItem 高度
_HEADER_HEIGHT = 36  # ProviderHeader 高度
_MIN_ITEMS = 3  # 最少显示 item 数
_MAX_ITEMS = 10  # 最多显示 item 数

# 成本金额：完整显示三项价格（不可裁剪）。等宽字体 + 名称等宽对齐实现起点一致，
# 金额自身不设窄固定宽（Minimum 自适应），保证 in/out/cache · $/M 全部可见。
_COST_MONO_FAMILY = "'Consolas', 'Segoe UI Mono', 'monospace'"
_COST_RIGHT_PAD = 2  # 行内右侧留白

# 滚动区域高度计算
_MIN_SCROLL_HEIGHT = _MIN_ITEMS * _ITEM_HEIGHT  # 最小高度：约 102px
_MAX_SCROLL_HEIGHT = _MAX_ITEMS * _ITEM_HEIGHT + _HEADER_HEIGHT  # 最大高度：约 274px


def _calculate_scroll_height(total_items: int) -> int:
    """根据 item 总数计算滚动区域高度"""
    if total_items <= _MIN_ITEMS:
        return _MIN_SCROLL_HEIGHT
    elif total_items >= _MAX_ITEMS:
        return _MAX_SCROLL_HEIGHT
    else:
        ratio = (total_items - _MIN_ITEMS) / (_MAX_ITEMS - _MIN_ITEMS)
        return int(_MIN_SCROLL_HEIGHT + ratio * (_MAX_SCROLL_HEIGHT - _MIN_SCROLL_HEIGHT))


class ProviderHeader(QWidget):
    """服务商标题行

    display_name：给用户看到的标题（可能带 " #2" 后缀区分同名配置）
    icon_provider_name：用于在 PROVIDER_ICONS 中查找图标的 key（一般是 base 服务商名）
    """

    def __init__(self, display_name: str, icon_provider_name: str = None, parent=None):
        super().__init__(parent)
        self.display_name = display_name
        self.icon_provider_name = icon_provider_name or display_name
        self.setFixedHeight(36)
        self.setStyleSheet("background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 8, 0)
        layout.setSpacing(8)

        # 服务商图标（按 icon_provider_name 查找，避免 " #2" 后缀让图标找不到）
        self.icon_widget = ProviderIconWidget(self.icon_provider_name, 20)
        layout.addWidget(self.icon_widget)

        # 服务商名称（显示用，含后缀）
        self.name_label = QLabel(self.display_name, self)
        self._apply_name_style()
        layout.addWidget(self.name_label)

        layout.addStretch(1)

    def _apply_name_style(self):
        Colors.refresh()
        self.name_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(12)}; font-weight: bold;"
        )


class ModelItem(QWidget):
    """单个模型项 - 可点击，模型名同行显示能力徽章、成本与描述"""

    clicked = pyqtSignal(str, str)  # provider_name, model_name

    # 能力徽章配色（文字胶囊：推理-琥珀 / 多模态-青靛）
    _THINK_TEXT = Colors.TAG_ORANGE_TEXT  # #ffc999
    _THINK_BG = "rgba(255,179,102,0.18)"
    _VISION_TEXT = Colors.TAG_ACCENT_TEXT  # #aae0ff
    _VISION_BG = "rgba(102,198,255,0.18)"

    def __init__(
        self,
        provider_name: str,
        model_name: str,
        is_active: bool = False,
        note: str = "",
        name_width: int = None,
        parent=None,
    ):
        super().__init__(parent)
        self.provider_name = provider_name
        self.model_name = model_name
        self.is_active = is_active
        self._note = note
        self._name_width = name_width
        self.setFixedHeight(34)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # 查询模型能力
        self._caps = self._get_caps()
        self._setup_ui()

    def _get_caps(self):
        """查询模型能力（thinking + vision）"""
        from app.core.model_capabilities import get_model_capabilities

        return get_model_capabilities(self.model_name)

    def _cost_text(self) -> str:
        """组装成本文本：{in}/{out}/{cache_read} · $/M。三值全无返回空串。"""
        cost = self._caps.get("cost") or {}
        vals = [cost.get("input"), cost.get("output"), cost.get("cache_read")]
        if not any(v is not None for v in vals):
            return ""
        parts = [_format_cost_number(v) if v is not None else "-" for v in vals]
        return f"{'/'.join(parts)} ·"

    def _cost_tooltip(self) -> str:
        """组装成本 tooltip 明细（含 cache_write）。无数据返回空串。"""
        cost = self._caps.get("cost") or {}
        rows = []
        for label, key in (
            ("Input", "input"),
            ("Output", "output"),
            ("Cache read", "cache_read"),
            ("Cache write", "cache_write"),
        ):
            v = cost.get(key)
            if v is not None:
                rows.append(f"{label}: ${_format_cost_number(v)}/M")
        return "\n".join(rows) if rows else ""

    def _make_cap_badge(self, text: str, text_color: str, bg_color: str, tip: str) -> QLabel:
        """构造能力徽章（文字胶囊，替换 emoji）"""
        lbl = QLabel(text, self)
        lbl.setStyleSheet(
            f"color: {text_color};"
            f"background-color: {bg_color};"
            f"border-radius: 4px; padding: 0 6px 0 6px;"
            f"font-weight: 600;"
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        lbl.setFixedHeight(18)
        lbl.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        lbl.setToolTip(tip)
        return lbl

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 12, 0)
        layout.setSpacing(6)

        # 选中态小圆点（U+2022，active 显示主题色；非 active 透明占位，保持列对齐）
        self.dot = QLabel("•", self)
        self.dot.setFixedWidth(14)
        layout.addWidget(self.dot)

        # 模型名（第一位的文本；组内有 trailing 信息时固定宽度 = 组内最长名，成本列对齐）
        self.name_label = QLabel(self.model_name, self)
        has_trailing = bool(
            self._cost_text() or self._caps.get("supports_thinking") or self._caps.get("supports_vision") or self._note
        )
        if has_trailing and self._name_width:
            self.name_label.setFixedWidth(self._name_width)
            self.name_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        else:
            self.name_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self._apply_name_style()
        layout.addWidget(self.name_label, 0)

        # 金额（对齐到组内最长模型名之后，跨模型比价）
        cost_text = self._cost_text()
        if cost_text:
            self.cost_label = QLabel(cost_text, self)
            self._apply_cost_style(active=self.is_active)
            self.cost_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            cost_tooltip = self._cost_tooltip()
            if cost_tooltip:
                self.cost_label.setToolTip(cost_tooltip)
            layout.addWidget(self.cost_label, 0)

        # 能力徽章（交互：思考 / 多模态），替换 emoji
        if self._caps.get("supports_thinking"):
            self.think_label = self._make_cap_badge("思考", self._THINK_TEXT, self._THINK_BG, "支持思考开关")
            layout.addWidget(self.think_label, 0)
        if self._caps.get("supports_vision"):
            self.vision_label = self._make_cap_badge("多模态", self._VISION_TEXT, self._VISION_BG, "支持多模态输入")
            layout.addWidget(self.vision_label, 0)

        # 描述 info（SVG question 图标，紧跟内容区，悬停显示完整描述）
        if self._note:
            self.info_icon = IconWidget(self)
            self.info_icon.setIcon(get_icon("question"))
            self.info_icon.setFixedSize(20, 20)
            self.info_icon.setToolTip(self._note)
            layout.addWidget(self.info_icon, 0)

        # 剩余空间推到最后（内容靠左自然排布，无右对齐顶行尾）
        layout.addStretch(1)

        # 应用选中态样式（dot + 名称，无整行填充）
        self._apply_dot_style()

    def _apply_cost_style(self, active: bool = None):
        """成本样式：完整显示三项金额（不得裁剪）。

        - 等宽字体保证各模型行金额起点/末位大致对齐比价
        - 不设窄 fixedWidth：Minimum 自适应展开，in/out/cache_read · $/M 全部可见
        active=True 时金额提亮为 TEXT_ACCENT，否则 TEXT_MUTED。
        建时/切换选中/主题刷新统一经此收敛。
        """
        if not hasattr(self, "cost_label"):  # 无成本模型不创建 cost_label，直接返回
            return
        Colors.refresh()
        active = self.is_active if active is None else active
        color = Colors.TEXT_ACCENT if active else Colors.TEXT_MUTED
        self.cost_label.setStyleSheet(f"color: {color};font-family: {_COST_MONO_FAMILY};{font_size_css(11)};")
        # 不设固定宽：让文本按内容自然展开，避免三项金额被裁剪
        self.cost_label.setMinimumWidth(0)
        self.cost_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)

    def _apply_dot_style(self):
        Colors.refresh()
        # 选中行才显示圆点；未选中行透明占位（不显示但不使列位移）
        color = Colors.TEXT_ACCENT if self.is_active else "transparent"
        self.dot.setStyleSheet(f"color: {color}; {get_font_family_css()} {font_size_css(18)}; font-weight: bold;")

    def _apply_name_style(self):
        Colors.refresh()
        if self.is_active:
            self.name_label.setStyleSheet(
                f"color: {Colors.TEXT_ACCENT}; font-weight: bold; {get_font_family_css()} {font_size_css(15)};"
            )
        else:
            self.name_label.setStyleSheet(
                f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(15)};"
            )

    def refresh_style(self):
        """主题切换后重刷 dot/名称/金额色（不重建 widget）。"""
        Colors.refresh()
        self._apply_dot_style()
        self._apply_name_style()
        self._apply_cost_style()

    def set_active(self, active: bool):
        self.is_active = active
        self._apply_dot_style()
        self._apply_name_style()
        self._apply_cost_style()

    def mousePressEvent(self, event):
        self.clicked.emit(self.provider_name, self.model_name)
        super().mousePressEvent(event)

    def enterEvent(self, event):
        if not self.is_active:
            self.name_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(15)};")
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._apply_name_style()
        super().leaveEvent(event)


class ModelSelectorCardContent(QWidget):
    """模型选择卡片内容"""

    modelSelected = pyqtSignal(str, str)  # provider_name, model_name
    stickyProviderChanged = pyqtSignal(str)  # 滚动时正在吸顶的服务商名（空字符串=无）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._provider_models: List[Tuple[str, List[str]]] = []
        self._current_provider: str = ""
        self._current_model: str = ""
        self._model_widgets: List[ModelItem] = []
        self._all_model_items: List[Tuple[ModelItem, str, str]] = []
        self._active_model_item: Optional[ModelItem] = None
        self._provider_headers: List[Tuple[QWidget, str]] = []  # (header_widget, provider_name)
        self._search_text = ""  # 搜索过滤文本，由标题栏搜索框设置
        self._model_notes: dict = {}  # 模型名 → 描述文本，搜索刷新时保留
        self._display_to_provider_name: dict = {}  # display_name → icon provider_name，搜索重建时保留
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._do_rebuild)
        self._pending_search_text = ""
        self._setup_ui()

    def _setup_ui(self):
        Colors.refresh()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 滚动区域
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self.scroll_area.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            {get_unified_scrollbar_style(8)}
        """)

        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background: transparent;")
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        # 底部弹性空间，让内容靠上
        self.content_layout.addStretch(1)

        self.scroll_area.setWidget(self.content_widget)

        # 连接滚动事件，更新吸顶服务商
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)

        layout.addWidget(self.scroll_area, 1)

    # ── 公有方法 ──────────────────────────────────────

    def set_search_filter(self, text: str):
        """外部设置搜索过滤文本（由标题栏搜索框调用）"""
        self._on_search_changed(text)

    def set_providers_data(
        self,
        provider_models: List[Tuple[str, List[str], bool]],  # (display_name, [models], is_current_provider)
        current_provider: str,
        current_model: str,
        display_to_provider_name: Optional[dict] = None,
        model_notes: Optional[dict] = None,  # 模型名 → 描述文本
    ):
        """设置服务商和模型数据

        provider_models 中的 provider_name 是 display_name（含 " #2" 后缀），
        用于显示和 ModelItem 内部 active 判定。
        display_to_provider_name: 可选映射，display_name → icon_provider_name，
        用于让 ProviderHeader 正确找到服务商图标（PROVIDER_ICONS 不识别后缀）。
        """
        # 重置滚动位置，避免重建后旧滚动位置导致吸顶服务商计算错误
        self.scroll_area.verticalScrollBar().setValue(0)
        self._current_provider = current_provider
        self._current_model = current_model
        self._provider_models = [(p, m) for p, m, _ in provider_models]
        self._model_notes = model_notes or {}
        self._display_to_provider_name = display_to_provider_name or {}
        self._model_widgets.clear()
        self._all_model_items.clear()
        self._provider_headers.clear()
        self._active_model_item = None

        # 清空内容区域（保留最后的 stretch）
        while self.content_layout.count() > 0:
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

        search_text = self._search_text
        name_map = display_to_provider_name or {}

        for provider_name, models, is_current_provider in provider_models:
            # 去重（保留首次出现的顺序），防止内部数据积累重复
            seen = set()
            deduped = []
            for m in models:
                key = m.strip().lower()
                if key not in seen:
                    seen.add(key)
                    deduped.append(m)
            # 过滤
            if search_text:
                filtered_models = [m for m in deduped if search_text in m.lower()]
                if not filtered_models:
                    continue
            else:
                filtered_models = deduped

            # 服务商标题：显示名是 display_name，图标查找用 icon_provider_name
            icon_name = name_map.get(provider_name, provider_name)
            header = ProviderHeader(provider_name, icon_name, self.content_widget)
            self.content_layout.addWidget(header)
            self._provider_headers.append((header, provider_name))

            # 该服务商内最长模型名宽度（模型名固定宽 → 金额列从同一 x 开始对齐比价）
            name_width = _measure_name_width(filtered_models)

            # 模型列表
            for model_name in filtered_models:
                is_active = provider_name == current_provider and model_name == current_model
                note = (model_notes or {}).get(model_name, "") if model_notes else ""
                item = ModelItem(provider_name, model_name, is_active, note, name_width, self.content_widget)
                if is_active:
                    self._active_model_item = item
                item.clicked.connect(self._on_model_clicked)
                self.content_layout.addWidget(item)
                self._model_widgets.append(item)
                self._all_model_items.append((item, provider_name, model_name))

        # 如果没有匹配的模型
        if not self._all_model_items and search_text:
            no_result = QLabel(f'未找到匹配 "{search_text}" 的模型', self.content_widget)
            no_result.setAlignment(Qt.AlignCenter)
            no_result.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(12)}; padding: 20px;"
            )
            self.content_layout.addWidget(no_result)

        # 底部弹性空间
        self.content_layout.addStretch(1)

        # 更新吸顶服务商显示
        self._update_sticky_header()

        # 延迟滚动到当前选中模型（等待布局稳定后再计算位置）
        if self._active_model_item is not None:
            scroll_target_provider = self._current_provider
            scroll_target_model = self._current_model
            # 使用短名保存避免闭包捕获 self._current_xxx 引用变化
            # 第一次尝试：0延迟，利用下一轮事件循环
            QTimer.singleShot(0, lambda p=scroll_target_provider, m=scroll_target_model: self._deferred_scroll(p, m))
            # 第二次尝试：50ms后，确保布局已完全稳定（首次打开卡片时需要更长时间）
            QTimer.singleShot(50, lambda p=scroll_target_provider, m=scroll_target_model: self._deferred_scroll(p, m))

    def refresh_style(self):
        """刷新主题样式"""
        Colors.refresh()
        self.content_widget.setStyleSheet("background: transparent;")
        # 刷新滚动区域样式（含滚动条颜色）
        self.scroll_area.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            {get_unified_scrollbar_style(8)}
        """)
        # 强制滚动条重新应用样式表（水平+垂直，确保主题切换后颜色即时生效）
        for sb in (self.scroll_area.verticalScrollBar(), self.scroll_area.horizontalScrollBar()):
            if sb is not None:
                sb_style = sb.style()
                if sb_style is not None:
                    sb_style.unpolish(sb)
                    sb_style.polish(sb)
        # 重刷每个 ModelItem（选中竖条/徽章/名称色跟随主题）
        for item in self._model_widgets:
            item.refresh_style()
        # 重新触发射信号，让标题栏标签更新颜色
        scroll_pos = self.scroll_area.verticalScrollBar().value()
        self._on_scroll(scroll_pos)

    # ── 内部方法 ──────────────────────────────────────

    def _clear_layout(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    def _scroll_to_item_center(self, item_widget: QWidget):
        """滚动滚动区域，使指定 item 居中显示"""
        scrollbar = self.scroll_area.verticalScrollBar()
        item_y = item_widget.pos().y()
        item_half = item_widget.height() // 2
        view_half = self.scroll_area.viewport().height() // 2
        target_scroll = item_y + item_half - view_half
        target_scroll = max(0, min(target_scroll, scrollbar.maximum()))
        scrollbar.setValue(target_scroll)

    def _deferred_scroll(self, provider_name: str, model_name: str):
        """延迟滚动：先处理事件确保布局稳定，再计算位置滚动"""
        # 先处理所有待处理的布局事件
        QApplication.processEvents()
        self._scroll_to_model(provider_name, model_name)

    def _scroll_to_model(self, provider_name: str, model_name: str):
        """根据服务商名和模型名找到对应 widget 并滚动居中"""
        for item, prov, model in self._all_model_items:
            if prov == provider_name and model == model_name:
                # 确保 item 已经有效布局（pos().y() 可能为 0，此时滚动无意义）
                if self.scroll_area.verticalScrollBar().maximum() > 0:
                    self._scroll_to_item_center(item)
                return

    def _on_scroll(self, value):
        """滚动条变化时更新吸顶服务商"""
        self._update_sticky_header()

    def _update_sticky_header(self):
        """根据当前滚动位置，发射当前吸顶服务商名称"""
        if not self._provider_headers:
            self.stickyProviderChanged.emit("")
            return

        scroll_pos = self.scroll_area.verticalScrollBar().value()

        # 找到最后一个被滚过顶部的服务商
        sticky_name = None
        for header_widget, provider_name in self._provider_headers:
            if header_widget.y() - scroll_pos <= 0:
                sticky_name = provider_name
            else:
                break

        self.stickyProviderChanged.emit(sticky_name or "")

    def _on_search_changed(self, text: str):
        """搜索文本变化时刷新列表（150ms 防抖）"""
        self._pending_search_text = text.strip().lower()
        self._search_timer.start()

    def _do_rebuild(self):
        """防抖到期后执行真正的列表重建"""
        self._search_text = self._pending_search_text
        provider_models_with_flag = []
        for prov, models in self._provider_models:
            is_cur = prov == self._current_provider
            provider_models_with_flag.append((prov, models, is_cur))

        self.set_providers_data(
            provider_models_with_flag,
            self._current_provider,
            self._current_model,
            self._display_to_provider_name,
            model_notes=self._model_notes,
        )

    def _on_model_clicked(self, provider_name: str, model_name: str):
        """模型被点击"""
        self.modelSelected.emit(provider_name, model_name)
