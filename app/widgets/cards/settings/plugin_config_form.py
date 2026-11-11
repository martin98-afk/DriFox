# -*- coding: utf-8 -*-
"""插件声明式配置表单（schema → 控件 + 即时保存 + 状态判定）。

从 PluginConfigCard 抽出的纯表单层：卡片（折叠壳）与插件详情页（常驻表单）
共用同一套字段渲染与保存逻辑，避免两处各写一份。

字段类型渲染映射：
    text/password → LineEdit / PasswordLineEdit
    textarea      → TextEdit（focusOut 触发 editingFinished）
    bool          → SwitchButton
    select        → SelectComboRow（ComboBox）
    number        → SpinBox
    model         → SelectComboRow（provider.list × model.enumerate 动态填充）
    link          → HyperlinkButton
    action        → PrimaryPushButton（ToolActionDialog 声明式编排）

语义：每个字段 editingFinished / checkedChanged 即时保存，无「保存」按钮；
空文本 = 清除（回 schema 默认或环境变量）。
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    HyperlinkButton,
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
    SpinBox,
    SwitchButton,
    TextEdit,
)

from loguru import logger

from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry


class _PlainEdit(TextEdit):
    """带 editingFinished 信号的多行输入（focus 离开时触发，与 LineEdit 语义一致）。

    继承 qfluentwidgets TextEdit（QSS 随深浅主题），而非 Qt 原生 QPlainTextEdit。
    """

    editingFinished = Signal()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.editingFinished.emit()


class SelectComboRow(QWidget):
    """select 字段下拉选择控件（qfluentwidgets ComboBox，随主题自适配）。

    API 兼容旧版胶囊/单选实现：currentData()/setCurrentData() 读写当前值，
    valueChanged 信号仅在用户切换时发射；setCurrentData 内部阻断信号，
    回显不会造成循环写盘。
    """

    valueChanged = Signal(object)

    def __init__(self, options, parent=None):
        """options: List[(value, label)]，保持 schema 声明顺序"""
        super().__init__(parent)
        self._options = list(options or [])
        self._values = [v for v, _ in self._options]
        self._current = None

        self._combo = ComboBox(self)
        for value, label in self._options:
            self._combo.addItem(text=str(label), userData=value)
        self._combo.currentIndexChanged.connect(self._on_index_changed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # 下拉撑满控件列（与输入框左右边缘对齐；不撑开时只有一个 sizeHint 宽度）
        layout.addWidget(self._combo, 1)

    def _on_index_changed(self, index: int) -> None:
        value = self._combo.itemData(index)
        if value != self._current:
            self._current = value
            self.valueChanged.emit(value)

    def currentData(self):
        return self._current

    def setCurrentData(self, value) -> None:
        if value not in self._values and self._values:
            value = self._values[0]
        self._current = value
        self._combo.blockSignals(True)
        self._combo.setCurrentIndex(self._values.index(value))
        self._combo.blockSignals(False)


class _FieldRow(QWidget):
    """字段行：左侧固定列宽 label，右侧控件按自身最大宽度停靠。

    label 列宽由表单按最长字段名统一计算（见 PluginConfigForm._calc_label_width），
    而不是每行各算各的：只要有一行 label 更长，控件左边缘就会整体右移，视觉上
    整张表单错位。控件也不撑满整行：数字/下拉只占需要的那点宽度。
    """

    def __init__(
        self,
        label_text: str,
        control: QWidget,
        parent=None,
        required: bool = False,
        label_width: int = 140,
        stretch_control: bool = True,
    ):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(20)

        # 必填星号拼进 label 文本：BodyLabel 走样式表配色，单独控件会丢主题色
        # label 右对齐：字段名长短不一时控件左边缘仍能对齐成一条竖线
        self._label = BodyLabel(f"{label_text} *" if required else label_text, self)
        self._label.setObjectName("fieldLabel")
        self._label.setFixedWidth(label_width)
        layout.addWidget(self._label, 0, Qt.AlignRight | Qt.AlignVCenter)
        if stretch_control:
            # 说明文字：占满控件列剩余宽度，超出换行
            layout.addWidget(control, 1)
        else:
            # 输入控件：紧跟 label，剩余空白放到最右（否则控件被 stretch 推到最右）
            layout.addWidget(control, 0, Qt.AlignLeft | Qt.AlignVCenter)
            layout.addStretch(1)


def evaluate_config_status(plugin_name: str) -> Tuple[str, list]:
    """配置完整度：('missing' | 'unconfigured' | 'ok' | 'none', 缺失字段 label 列表)

    - missing：schema 声明了 required 且当前生效值为空
    - unconfigured：未声明 required 的老插件，可填字段（text/password/textarea）全空
    - ok：其余情况

    设置页排序、卡片副标题、详情页状态都读这一处判定，避免多套口径漂移。
    一次读盘取全部生效值（effective_values），不逐字段 get()。
    """
    schema = PluginConfigRegistry.get_instance().get(plugin_name)
    if schema is None:
        return ("none", [])
    if not schema.fields:
        return ("ok", [])
    try:
        values = PluginConfigStore().effective_values(plugin_name)
    except Exception as e:  # noqa: BLE001 - 状态判定失败按正常处理，不阻断表单
        logger.debug(f"[PluginConfigForm] {plugin_name} 配置状态判定失败: {e}")
        return ("ok", [])
    # bool/link/action 无「空值」语义：布尔没有必填概念，后两者不落存储
    requires = [f for f in schema.fields if getattr(f, "required", False) and f.type not in ("bool", "link", "action")]
    if requires:
        missing = [f.label for f in requires if not str(values.get(f.key) or "").strip()]
        return ("missing", missing) if missing else ("ok", [])
    fillable = [f for f in schema.fields if f.type in ("text", "password", "textarea")]
    if fillable and all(not str(values.get(f.key) or "").strip() for f in fillable):
        return ("unconfigured", [])
    return ("ok", [])


class PluginConfigForm(QWidget):
    """schema 驱动的配置表单：字段控件 + 回显 + 即时保存 + 状态判定。"""

    valueChanged = Signal(str)  # 字段 key（保存成功后发射，供外层刷新状态）

    def __init__(self, plugin_name: str, parent=None):
        super().__init__(parent)
        self._plugin_name = plugin_name
        self._rows: Dict[str, QWidget] = {}
        self._bool_switches: Dict[str, SwitchButton] = {}
        # 文本字段回显基线：记录最近一次 setText 的值，用于判断"内容是否真正变化"
        self._echoed: Dict[str, str] = {}
        # 回显/程序化设值期间置位，阻断信号触发 _on_field_changed（防循环写盘）
        self._echoing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)  # 字段块间距由 _add_field_row 显式控制
        self._layout = layout

        schema = PluginConfigRegistry.get_instance().get(plugin_name)
        if schema is None:
            return
        self._label_width = self._calc_label_width(schema)
        for f in schema.fields:
            self._build_field(f)
        self.echo()

    def _calc_label_width(self, schema) -> int:
        """按最长字段名算统一列宽（120~200px）

        字号跟随设置页配置，所以用 QFontMetrics 实测而不是写死像素。
        """
        try:
            from PySide6.QtGui import QFont, QFontMetrics

            from app.utils.design_tokens import get_ui_font_size, scale_font_size

            font = QFont()
            font.setPixelSize(scale_font_size(get_ui_font_size()))
            metrics = QFontMetrics(font)
            widths = [
                metrics.horizontalAdvance(f.label + (" *" if getattr(f, "required", False) else ""))
                for f in schema.fields
                if f.type != "bool"
            ]
            return max(120, min(200, max(widths, default=0) + 16))
        except Exception:
            return 140

    # ── 字段构建 ──────────────────────────────────────────

    def _build_field(self, f) -> None:
        if f.type == "bool":
            switch = SwitchButton(self)
            self._bool_switches[f.key] = switch
            self._rows[f.key] = switch
            switch.setOnText("")
            switch.setOffText("")
            switch.checkedChanged.connect(lambda _checked, _k=f.key: self._on_field_changed(_k))
            self._add_field_row(
                f,
                _FieldRow(
                    f.label,
                    switch,
                    self,
                    required=f.required,
                    label_width=self._label_width,
                    stretch_control=False,
                ),
            )
        elif f.type == "select":
            # 下拉选择：点击选中即保存（SelectComboRow）
            combo = SelectComboRow(f.options, self)
            combo.valueChanged.connect(lambda _v, _k=f.key: self._on_field_changed(_k))
            self._rows[f.key] = combo
            self._add_field_row(f, _FieldRow(f.label, combo, self, required=f.required, label_width=self._label_width))
        elif f.type == "model":
            # 模型选择下拉：选项走 provider.list × model.enumerate 能力动态填充，
            # 首项「跟随当前会话模型」= 空值；存储与保存语义同 select
            combo = SelectComboRow(self._load_model_options(), self)
            combo.valueChanged.connect(lambda _v, _k=f.key: self._on_field_changed(_k))
            self._rows[f.key] = combo
            self._add_field_row(f, _FieldRow(f.label, combo, self, required=f.required, label_width=self._label_width))
        elif f.type == "number":
            spin = SpinBox(self)
            spin.setRange(f.min if f.min is not None else 0, f.max if f.max is not None else 2147483647)
            spin.setSingleStep(max(1, f.step))
            spin.valueChanged.connect(lambda *_a, _k=f.key: self._on_field_changed(_k))
            self._rows[f.key] = spin
            self._add_field_row(f, _FieldRow(f.label, spin, self, required=f.required, label_width=self._label_width))
        elif f.type == "link":
            # 外链按钮：HyperlinkButton（主题色 + 链接图标），点击用系统浏览器打开。
            # 按钮自身是居中对齐文本，撑满整行会飘到右边，用 stretch 尾巴把它钉在左侧
            link_text = f.placeholder or f.url
            link = HyperlinkButton(f.url, link_text, self, FluentIcon.LINK)
            holder = self._left_aligned(link)
            self._add_field_row(
                f,
                _FieldRow(f.label, holder, self, required=f.required, label_width=self._label_width, stretch_control=False),
            )
        elif f.type == "action":
            # 动作按钮：点击弹窗执行声明式工具编排（通用机制，见 ToolActionDialog），
            # 无存储值不参与保存/回显
            btn = PrimaryPushButton(f.placeholder or f.label, self)
            btn.clicked.connect(lambda _c, _f=f, _btn=btn: self._open_action_dialog(_f, _btn))
            # 按钮不自撑满（撑满后文字居中、显得像主操作按钮），包一层靠左
            holder = self._left_aligned(btn)
            self._add_field_row(
                f,
                _FieldRow(f.label, holder, self, required=f.required, label_width=self._label_width, stretch_control=False),
            )
        elif f.type == "textarea":
            edit = _PlainEdit(self)
            if f.placeholder:
                edit.setPlaceholderText(f.placeholder)
            edit.setFixedHeight(max(2, f.rows) * 22 + 8)
            edit.editingFinished.connect(lambda _k=f.key: self._on_field_changed(_k))
            self._rows[f.key] = edit
            self._add_field_row(f, _FieldRow(f.label, edit, self, required=f.required, label_width=self._label_width))
        else:
            edit = PasswordLineEdit() if f.type == "password" else LineEdit()
            edit.setClearButtonEnabled(True)
            if f.placeholder:
                edit.setPlaceholderText(f.placeholder)
            self._rows[f.key] = edit
            edit.editingFinished.connect(lambda _k=f.key: self._on_field_changed(_k))
            self._add_field_row(f, _FieldRow(f.label, edit, self, required=f.required, label_width=self._label_width))

    def _left_aligned(self, control: QWidget) -> QWidget:
        """按钮类控件包一层靠左容器（按钮自身撑满会居中，视觉上像主操作）"""
        holder = QWidget(control.parentWidget())
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addWidget(control)
        holder_layout.addStretch(1)
        return holder

    def _load_model_options(self) -> tuple:
        """模型下拉选项：首项「跟随当前会话模型」+ provider.list × model.enumerate。

        能力授权按本插件 manifest 裁剪；能力不可用（旧宿主 / 无活跃窗口 /
        未声明能力）时只剩首项，下拉仍可用（存空值 = 跟随会话模型）。
        """
        options: list = [("", "跟随当前会话模型")]
        seen: set = {""}
        try:
            from app.plugins.capabilities.resolve import caps_for

            list_providers = caps_for("provider.list", plugin=self._plugin_name, surface="ui")
            enumerate_models = caps_for("model.enumerate", plugin=self._plugin_name, surface="ui")
            if not callable(list_providers) or not callable(enumerate_models):
                return tuple(options)
            for p in list_providers() or []:
                if not isinstance(p, dict):
                    continue
                display = str(p.get("display_name") or p.get("id") or "").strip()
                pid = str(p.get("id") or "").strip()
                if not display or not pid:
                    continue
                for m in enumerate_models(pid) or []:
                    if not isinstance(m, dict):
                        continue
                    name = str(m.get("name") or "").strip()
                    if not name:
                        continue
                    value = f"{display}:{name}"
                    if value in seen:
                        continue
                    seen.add(value)
                    options.append((value, value))
        except Exception as e:  # noqa: BLE001 - 选项加载失败不阻断卡构建
            logger.debug(f"[PluginConfigForm] 模型选项加载失败: {e}")
        return tuple(options)

    @staticmethod
    def _style_hint(label: CaptionLabel) -> None:
        """说明文字：比正文小一号、用主题次要色、限宽换行

        CaptionLabel 自带颜色是纯黑/纯白（不随自定义主题），必须显式覆盖；
        字号也不能跟正文同级，否则整张表单没有层级。
        """
        from app.utils.design_tokens import Colors, get_ui_font_size, scale_font_size

        px = scale_font_size(max(11, get_ui_font_size() - 3))
        label.setObjectName("configHint")
        label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {px}px;")
        label.setMaximumWidth(760)

    def _add_field_row(self, f, row: QWidget) -> None:
        """一个字段 = 一行控件 +（可选）说明，整块与相邻字段拉开间距

        说明文字用 CaptionLabel 而不自设颜色：设计令牌的 TEXT_SECONDARY 是
        深色主题值（白色半透明），浅色主题下写了就等于隐形；CaptionLabel 自带
        随主题变化的次要文字样式。说明行用同样的 label 列宽占位，左边缘与控件对齐。
        """
        block = QWidget(self)
        box = QVBoxLayout(block)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        box.addWidget(row)
        if f.description:
            hint = CaptionLabel(f.description, block)
            hint.setWordWrap(True)
            self._style_hint(hint)
            box.addWidget(_FieldRow("", hint, block, label_width=self._label_width, stretch_control=True))
        self._layout.addWidget(block)
        self._layout.addSpacing(12)

    # ── 回显与保存 ────────────────────────────────────────

    def echo(self) -> None:
        """回显当前生效值（默认兜底可见）；程序化设值期间置 _echoing 阻断信号循环"""
        store = PluginConfigStore()
        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return
        self._echoing = True
        try:
            for f in schema.fields:
                control = self._rows.get(f.key)
                if control is None:
                    continue
                val = store.get(self._plugin_name, f.key)
                self._apply_value(control, f, val)
        finally:
            self._echoing = False

    def _apply_value(self, control: QWidget, f, val) -> None:
        """按字段类型把存储值设置到控件 + 记录回显基线（_echoing 置位期间调用）"""
        if f.type == "bool":
            control.setChecked(bool(val))
        elif f.type in ("select", "model"):
            control.setCurrentData(val)
            self._echoed[f.key] = control.currentData()
        elif f.type == "number":
            try:
                control.setValue(int(val))
            except (TypeError, ValueError):
                control.setValue(f.min if f.min is not None else 0)
            self._echoed[f.key] = str(control.value())
        elif f.type == "textarea":
            text = str(val if val is not None else "")
            control.setPlainText(text)
            self._echoed[f.key] = text
        else:
            text = str(val if val is not None else "")
            control.setText(text)
            self._echoed[f.key] = text

    def _on_field_changed(self, key: str) -> None:
        """单字段即时保存：空文本=清除（回默认），保存后刷新回显。

        用 editingFinished / checkedChanged / currentIndexChanged / valueChanged 触发，
        自动持久化，无需底部"保存配置"按钮。
        仅当内容真正变化时才写盘：editingFinished 在聚焦→失焦（未修改）时
        也会触发，若此时无脑写回输入框回显值，会覆盖用户手动编辑 config.json
        或其他实例的修改（双实例共享配置场景）。
        """
        if self._echoing:
            return
        store = PluginConfigStore()
        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return
        target_field = next((f for f in schema.fields if f.key == key), None)
        if target_field is None:
            return
        control = self._rows.get(key)
        if control is None:
            return
        if target_field.type == "bool":
            value: object = control.isChecked()
        elif target_field.type in ("select", "model"):
            value = control.currentData()
        elif target_field.type == "number":
            value = str(control.value())
        elif target_field.type == "textarea":
            value = control.toPlainText().strip()
        else:
            value = control.text().strip()
            # 无变化 → 跳过写盘：editingFinished 在聚焦→失焦（未修改）时也会触发，
            # 若输入框内容与回显基线一致（用户没真正编辑），写回会覆盖
            # 外部/其他实例对 config.json 的手动修改（双实例共享配置场景）。
            if value == self._echoed.get(key, ""):
                return
        store.set_values(self._plugin_name, {key: value})
        # 非 bool 字段保存后回显（清除 → 默认值显式可见）
        if target_field.type != "bool":
            self._echo_field(key)
        # 网关插件 enabled 开关：切换即触发平台连接启停（Phase E 删 GatewaySettingCard
        # 后补回交互——旧卡开关联动 manager.start/stop_platform，通用卡只写配置会导致
        # “开启没反应”）。仅对该插件注册的网关平台生效；manager 未初始化时交由启动期
        # start_all_async 读取最新配置兜底。
        if target_field.type == "bool" and self._is_gateway_plugin():
            self._apply_gateway_toggle(bool(value))
        self.valueChanged.emit(key)

    def _is_gateway_plugin(self) -> bool:
        """本插件是否注册了 gateway 平台（按 def.source == 'plugin:<name>' 判定）"""
        try:
            from app.plugins.registries.gateway_platform_registry import (
                GatewayPlatformRegistry,
            )

            src = f"plugin:{self._plugin_name}"
            return any(d.source == src for d in GatewayPlatformRegistry.get_instance().list_platforms())
        except Exception:
            return False

    def _apply_gateway_toggle(self, enabled: bool) -> None:
        """enabled 切换 → 对插件注册的每个网关平台启停连接。

        manager 未初始化（网关后台线程尚未建单例）时静默跳过：启动期
        start_all_async 会读最新配置自动连接。
        """
        try:
            from app.gateway.manager import get_platform_manager
            from app.plugins.registries.gateway_platform_registry import (
                GatewayPlatformRegistry,
            )

            mgr = get_platform_manager()
            if mgr is None:
                return
            src = f"plugin:{self._plugin_name}"
            for d in GatewayPlatformRegistry.get_instance().list_platforms():
                if d.source != src:
                    continue
                if enabled:
                    mgr.start_platform_async(d.platform_id)
                else:
                    mgr.stop_platform(d.platform_id)
        except Exception as e:
            logger.warning(f"[PluginConfigForm] 网关启停失败 {self._plugin_name}: {e}")

    def _echo_field(self, key: str) -> None:
        """单字段回显（保存后刷新显示；_echoing 阻断信号循环）"""
        store = PluginConfigStore()
        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        if schema is None:
            return
        target_field = next((f for f in schema.fields if f.key == key), None)
        if target_field is None or target_field.type == "bool":
            return
        control = self._rows.get(key)
        if control is None:
            return
        val = store.get(self._plugin_name, key)
        self._echoing = True
        try:
            self._apply_value(control, target_field, val)
        finally:
            self._echoing = False

    # ── 状态判定与搜索 ────────────────────────────────────

    def config_status(self) -> Tuple[str, list]:
        """配置完整度判定（委托模块级 evaluate_config_status，与插件页同一口径）"""
        return evaluate_config_status(self._plugin_name)

    def matches(self, keyword: str) -> bool:
        """搜索命中：插件名 / 配置块标题 / 字段 label、key、说明（空关键字全命中）"""
        kw = (keyword or "").strip().lower()
        if not kw:
            return True
        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        haystack = [self._plugin_name, schema.title if schema else ""]
        if schema is not None:
            for f in schema.fields:
                haystack.extend((f.label, f.key, f.description))
        return any(kw in str(text).lower() for text in haystack if text)
