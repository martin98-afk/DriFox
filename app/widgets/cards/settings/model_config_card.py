# -*- coding: utf-8 -*-
"""
模型配置卡片 - 优化布局，有变化自动保存

布局策略（set_config 渲染时）：
  1. 字段按 PARAM_SCHEMA.order 排序
  2. 字段按功能分组（_FIELD_GROUPS）：上下文 / 思考 / 采样
  3. 每组有 subtle 标题，组间额外间距
  4. 标签最小宽度 80px，右边控件对齐
  5. 渲染完后根据字段数估算内容高度，调整父 BaseSettingsCard 的高度
"""
import webbrowser

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    LineEdit,
    PasswordLineEdit,
    Slider,
    SpinBox,
    SwitchButton,
)

from app.constants import PARAM_SCHEMA
from app.constants import PROVIDER_MANAGED_KEYS
from app.constants import provider_quota_exclude_keys as QUOTA_EXCLUDE_KEYS
from app.utils.design_tokens import Colors, font_size_css
from app.widgets.cards.settings.base_settings_card import BaseSettingsCard
from app.widgets.searchable_editable_combobox import SearchableEditableComboBox

# =============================================================================
# 字段分组：定义显示顺序与分类
# key 在哪个元组里就归到哪个组；不在任何组里的会归到"其他"（一般不会出现）
# =============================================================================
_FIELD_GROUPS = [
    ("上下文", ("最大Token", "上下文长度", "最大输出")),
    ("思考",   ("思考模式", "思考预算", "思考等级")),
    ("采样",   ("温度", "temp", "top_p", "max_new_tokens")),
    ("能力声明", ("声明_支持思考", "声明_支持图像", "声明_上下文长度", "声明_最大输出", "声明_思考参数", "声明_思考强度")),
]

# =============================================================================
# 高度估算（px）
# =============================================================================
_FIELD_ROW_HEIGHT = 34        # 每个字段行（label + widget）
_GROUP_HEADER_HEIGHT = 22     # 分组标题
_GROUP_SPACING = 14           # 组间额外间距
_CONTENT_PADDING = 12         # 内容区上下内边距
_CARD_HEADER_HEIGHT = 30      # BaseSettingsCard 头部（图标+标题+关闭）
_MIN_CARD_HEIGHT = 240        # 卡片最小高度
_MAX_CARD_HEIGHT = 460        # 卡片最大高度
_LABEL_MIN_WIDTH = 80         # 标签最小宽度（让控件对齐）


class ModelConfigCard(QWidget):
    """模型配置卡片内容 - 有变化自动保存"""

    configApplied = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = {}
        self.current_provider = ""
        self.current_model_name = ""
        self._widgets = {}
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)
        self._save_timer.timeout.connect(self._do_save)
        self._setup_ui()

    def _setup_ui(self):
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(8, 8, 8, 8)
        self.layout.setSpacing(6)

    def _clear_layout(self, layout):
        """递归清理 layout"""
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    # ------------------------------------------------------------------
    # 字段分组
    # ------------------------------------------------------------------
    def _group_items(self, items):
        """按 _FIELD_GROUPS 顺序分组，未匹配的归到"其他"组"""
        used_keys = set()
        groups = []
        for group_name, group_keys in _FIELD_GROUPS:
            group_items = [it for it in items if it[1] in group_keys]
            for it in group_items:
                used_keys.add(it[1])
            if group_items:
                groups.append((group_name, group_items))
        ungrouped = [it for it in items if it[1] not in used_keys]
        if ungrouped:
            groups.append(("其他", ungrouped))
        return groups

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def set_config(self, title: str, config: dict, model_name: str = ""):
        self.current_provider = title
        self.current_model_name = model_name or ""

        # 连接信息 + 系统字段（不渲染到参数列表中）
        skip_keys = {
            "模型名称", "API_URL", "API_KEY", "认证方式", "获取地址",
            "选择模型", "provider_name", "name", "config_id",
            "display_name", "_suffix_index",
            *QUOTA_EXCLUDE_KEYS(),  # 套餐用量查询字段不渲染到参数列表
            *PROVIDER_MANAGED_KEYS,  # 服务商级管理键（模型列表生命周期，非模型参数）
        }

        # ⚠ 过滤必须发生在入口：get_config 从 self.config.copy() 起步全量回传，
        # 任何留在 self.config 里的键都会被 emit 出去并写回磁盘配置。
        # 只挡渲染不够——服务商管理键（bool / 时间戳）经此往返会被改坏。
        self.config = {k: v for k, v in config.items() if k not in skip_keys}

        self._clear_layout(self.layout)
        self._widgets.clear()

        # 收集要渲染的字段：[(order, key, value, meta), ...]
        items = []
        seen_display_names = set()
        for key, value in config.items():
            if key in skip_keys:
                continue
            meta = PARAM_SCHEMA.get(key, {})
            if meta.get("hide_in_card"):
                continue
            # 思考等级：仅当模型是 reasoning_effort 型且 models.dev 给出了 effort
            # 可选值（reasoning_effort_values）才显示——无 values = 不支持调整强度，
            # toggle/budget 型思考模型也没有"强度"概念，一并隐藏该配置项
            if key == "思考等级" and self.current_model_name:
                from app.core.modelmeta.model_capabilities import get_model_capabilities

                caps = get_model_capabilities(self.current_model_name)
                if caps.get("thinking_param") != "reasoning_effort" or not caps.get("reasoning_effort_values"):
                    continue
            display_name = meta.get("display_name", key)
            if display_name in seen_display_names:
                continue
            seen_display_names.add(display_name)
            order = meta.get("order", 999)
            items.append((order, key, value, meta))

        items.sort(key=lambda x: x[0])

        # 声明键常驻渲染（P1-9 段三）：config 里没有也要显示——默认「自动」/空
        # = 未声明。不常驻的话用户永远看不到声明入口。
        existing_keys = {it[1] for it in items}
        for decl_key, decl_meta in PARAM_SCHEMA.items():
            if decl_key.startswith("声明_") and decl_key not in existing_keys and not decl_meta.get("hide_in_card"):
                items.append((decl_meta.get("order", 999), decl_key, None, decl_meta))

        groups = self._group_items(items)

        # 渲染各组
        is_first_group = True
        for group_name, group_items in groups:
            if not group_items:
                continue
            if not is_first_group:
                self.layout.addSpacing(_GROUP_SPACING)
            is_first_group = False

            # 分组标题
            header = BodyLabel(group_name, self)
            header.setStyleSheet(
                f"color: {Colors.TEXT_SECONDARY}; "
                f"font-size: 11px; font-weight: 600; "
                f"padding: 0 0 4px 2px;"
            )
            self.layout.addWidget(header)
            # 能力声明组：有生效声明时组头风险提示（声明值压制自动探测，
            # 异常声明会覆盖真实能力——文档 §6.11）
            if group_name == "能力声明":
                has_declared = any(v not in (None, "") for _o, _k, v, _m in group_items)
                warn = BodyLabel(
                    "已声明：值优先于模型自动探测，异常值会覆盖真实能力"
                    if has_declared
                    else "留空/自动 = 跟随模型数据（models.dev / 服务商声明）",
                    self,
                )
                warn.setStyleSheet(
                    f"color: {'#e05656' if has_declared else Colors.TEXT_MUTED}; "
                    f"{font_size_css(10)}; padding: 0 0 2px 2px; background: transparent; border: none;"
                )
                self.layout.addWidget(warn)

            # 字段行
            for _order, key, value, meta in group_items:
                ui_type = meta.get("ui_type") or self._infer_fallback_type(key, value)
                widget = self._create_widget(key, ui_type, value, meta)
                display_name = meta.get("display_name", key)
                label = BodyLabel(f"{display_name}：", self)
                label.setMinimumWidth(_LABEL_MIN_WIDTH)
                hlayout = QHBoxLayout()
                hlayout.setContentsMargins(0, 0, 0, 0)
                hlayout.setSpacing(8)
                hlayout.addWidget(label, 0)
                hlayout.addWidget(widget, 1)
                # 声明键未声明时：行尾小字标自动链生效层（P1-9 段三）
                if key.startswith("声明_") and value in (None, "") and self.current_model_name:
                    try:
                        from app.core.modelmeta.model_capabilities import (
                            DECLARED_CAPABILITY_KEYS,
                            get_model_capability_sources,
                        )

                        field = DECLARED_CAPABILITY_KEYS.get(key, "")
                        src = get_model_capability_sources(self.current_model_name).get(field)
                        src_text = {"caps": "模型数据", "family": "服务商声明", "default": "内置默认"}.get(src)
                        if src_text:
                            hint = BodyLabel(f"自动: {src_text}", self)
                            hint.setStyleSheet(
                                f"color: {Colors.TEXT_MUTED}; {font_size_css(10)}; "
                                f"background: transparent; border: none;"
                            )
                            hlayout.addWidget(hint, 0)
                    except Exception:
                        pass
                self.layout.addLayout(hlayout)
                self._widgets[key] = (label, widget)

        # 估算内容高度并调整父 BaseSettingsCard 的高度
        self._adjust_parent_height(items, groups)

    def _adjust_parent_height(self, items, groups):
        """根据字段数和组数估算高度，向上找 BaseSettingsCard 并设置最小高度
        最大高度由 SystemCardFrame.showEvent 根据窗口高度自适应控制"""
        field_count = len(items)
        non_empty_groups = [g for _, g in groups if g]
        group_count = len(non_empty_groups)
        group_separator_count = max(0, group_count - 1)

        content_height = (
            _CONTENT_PADDING * 2
            + group_count * _GROUP_HEADER_HEIGHT
            + field_count * _FIELD_ROW_HEIGHT
            + group_separator_count * _GROUP_SPACING
        )
        card_height = _CARD_HEADER_HEIGHT + content_height
        card_height = max(_MIN_CARD_HEIGHT, min(_MAX_CARD_HEIGHT, card_height))

        # 沿父链向上找 BaseSettingsCard
        parent = self.parentWidget()
        while parent:
            if isinstance(parent, BaseSettingsCard):
                parent.setMinimumHeight(int(card_height))
                break
            parent = parent.parentWidget()

    def _infer_fallback_type(self, key: str, value) -> str:
        """对 schema 未收录的键做启发式猜测"""
        key_lower = key.lower()
        if "key" in key_lower or ("token" in key_lower and key not in ["最大Token", "上下文长度"]):
            return "password"
        # ⚠ bool 必须排在 int/float 之前：bool 是 int 的子类，
        # isinstance(True, int) 为真，且 True == 1 落在 0~2 区间，
        # 会被下面的 slider 分支命中渲染成滑条（值拖动后变 float，覆盖原 bool）。
        if isinstance(value, bool):
            return "checkbox"
        if isinstance(value, (int, float)):
            if 0 <= value <= 2:
                return "slider"
            return "spinbox"
        return "line"

    def _create_widget(self, key, ui_type: str, value, meta: dict):
        if ui_type == "password":
            widget = PasswordLineEdit(self)
            widget.setText(str(value) if value else "")
            widget.setMinimumWidth(280)
            widget.textChanged.connect(lambda: self._on_field_changed())
            return widget

        elif ui_type == "slider":
            range_info = meta.get(
                "range", {"min": 0.0, "max": 1.0, "step": 0.01, "type": "float"}
            )
            min_val = range_info["min"]
            max_val = range_info["max"]
            step = range_info["step"]
            is_float = range_info["type"] == "float"
            current = float(value) if value not in (None, "") else min_val
            scale = 1 / step
            slider_min = int(min_val * scale)
            slider_max = int(max_val * scale)
            slider_value = int(round(current * scale))

            container = QWidget(self)
            container.setFixedHeight(28)
            hlayout = QHBoxLayout(container)
            hlayout.setContentsMargins(0, 0, 0, 0)

            slider = Slider(Qt.Horizontal, self)
            slider.setRange(slider_min, slider_max)
            slider.setValue(slider_value)
            slider.setMinimumHeight(22)
            slider.valueChanged.connect(lambda: self._on_field_changed())

            display_value = current if is_float else int(current)
            label = BodyLabel(
                f"{display_value:.2f}" if is_float else str(int(display_value)), self
            )
            label.setFixedWidth(60)
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

            def _update_label(v):
                logical_val = v / scale
                if not is_float:
                    logical_val = int(logical_val)
                fmt_val = f"{logical_val:.2f}" if is_float else str(logical_val)
                label.setText(fmt_val)

            slider.valueChanged.connect(_update_label)

            hlayout.addWidget(slider, 1)
            hlayout.addWidget(label)

            container.slider = slider
            container.label = label
            container.range_info = range_info
            container.scale = scale

            return container

        elif ui_type == "checkbox":
            widget = SwitchButton(self)
            widget._onText = widget.tr("开启")
            widget._offText = widget.tr("关闭")
            checked = False
            if isinstance(value, bool):
                checked = value
            elif isinstance(value, str):
                checked = value.lower() in ("true", "1", "yes", "on")
            elif isinstance(value, (int, float)):
                checked = bool(value)
            widget.setChecked(checked)
            widget.checkedChanged.connect(lambda: self._on_field_changed())
            return widget

        elif ui_type == "tri_state":
            # 声明键三态（P1-9 段三）：自动/开启/关闭。「自动」= 未声明
            # （get_config 删键，防 copy 残留）；键缺失/空值回显「自动」
            widget = ComboBox(self)
            widget.addItems(["自动", "开启", "关闭"])
            if isinstance(value, bool) or value in (0, 1):
                widget.setCurrentText("开启" if value else "关闭")
            elif isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on", "是"):
                widget.setCurrentText("开启")
            elif isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off", "否"):
                widget.setCurrentText("关闭")
            else:
                widget.setCurrentText("自动")
            widget.setMinimumWidth(280)
            widget.currentTextChanged.connect(lambda: self._on_field_changed())
            return widget

        elif ui_type == "lineedit_int":
            # 声明数值键：允许留空（空 = 未声明，get_config 删键）
            widget = LineEdit(self)
            widget.setPlaceholderText("自动")
            widget.setText(str(value) if value not in (None, "") else "")
            widget.setMinimumWidth(280)
            widget.textChanged.connect(lambda: self._on_field_changed())
            return widget

        elif ui_type == "combobox":
            widget = ComboBox(self)
            options = list(meta.get("options", []))
            # 思考等级：以 models.dev 模型能力为准——下拉选项来自该模型
            # reasoning_options 中 effort 的 values（如 ["high", "max"]）；
            # models.dev 无数据时回退 PARAM_SCHEMA 固定默认。
            if key == "思考等级" and self.current_model_name:
                from app.core.modelmeta.model_capabilities import get_model_capabilities

                caps = get_model_capabilities(self.current_model_name)
                dyn_values = caps.get("reasoning_effort_values")
                if dyn_values:
                    options = list(dyn_values)
            widget.addItems(options)
            current = str(value) if value else ""
            if current in options:
                widget.setCurrentText(current)
            elif options:
                # 保存值不在当前模型可选值中（切模型后残留）→ 强制回退中间配置
                # （仅思考等级按此规则；其余 combobox 字段保持回退首项）
                if key == "思考等级":
                    widget.setCurrentText(options[(len(options) - 1) // 2])
                else:
                    widget.setCurrentText(options[0])
            widget.setMinimumWidth(280)
            widget.currentTextChanged.connect(lambda: self._on_field_changed())
            return widget

        elif ui_type == "spinbox":
            widget = SpinBox()
            val = int(value) if value else 2048
            range_info = meta.get("range", {"min": 1, "max": 99999999})
            widget.setRange(range_info["min"], range_info["max"])
            widget.setValue(val)
            widget.setMinimumWidth(280)
            widget.valueChanged.connect(lambda: self._on_field_changed())
            return widget

        else:
            widget = LineEdit(self)
            widget.setMinimumWidth(280)
            widget.setText(str(value) if value else "")
            widget.textChanged.connect(lambda: self._on_field_changed())
            return widget

    def _on_field_changed(self):
        self._save_timer.start()

    def _do_save(self):
        config = self.get_config()
        self.configApplied.emit(config)

    def get_config(self) -> dict:
        result = self.config.copy()
        for key, (label, widget) in self._widgets.items():
            actual_key = "模型名称" if key == "选择模型" else key

            if isinstance(widget, LineEdit):
                text = widget.text().strip()
                if not text and PARAM_SCHEMA.get(key, {}).get("ui_type") == "lineedit_int":
                    # 声明数值键留空 = 未声明 → 删键（防 copy 残留旧值假声明）
                    result.pop(actual_key, None)
                else:
                    result[actual_key] = text
            elif isinstance(widget, ComboBox):
                if PARAM_SCHEMA.get(key, {}).get("ui_type") == "tri_state":
                    # 声明三态：「自动」= 未声明 → 删键（最关键一行，防 copy 残留）
                    if widget.currentText() == "自动":
                        result.pop(actual_key, None)
                    else:
                        result[actual_key] = widget.currentText() == "开启"
                else:
                    result[actual_key] = widget.currentText()
            elif isinstance(widget, SearchableEditableComboBox):
                text = (
                    widget.text().strip()
                    if callable(getattr(widget, "text", None))
                    else ""
                )
                if text:
                    result[actual_key] = text
                else:
                    result[actual_key] = (
                        widget.currentText() if hasattr(widget, "currentText") else ""
                    )
            elif hasattr(widget, "slider"):
                logical_value = widget.slider.value() / widget.scale
                range_info = getattr(widget, "range_info", {})
                if range_info.get("type") == "int":
                    result[actual_key] = int(round(logical_value))
                else:
                    result[actual_key] = float(logical_value)
            elif isinstance(widget, SpinBox):
                result[actual_key] = widget.value()
            elif hasattr(widget, "isChecked"):
                result[actual_key] = widget.isChecked()
            else:
                result[actual_key] = ""
        return result

