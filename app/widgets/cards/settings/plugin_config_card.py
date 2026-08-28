# -*- coding: utf-8 -*-
"""声明式插件配置自动渲染卡（E1）。

折叠卡 + 字段独立保存（重写）：
- 一张 ExpandSettingCard，标题 = schema.title，副标题 = 字段数概览
- 展开后内部用表单：每行 label + LineEdit（password → PasswordLineEdit）
- bool 字段以 SwitchButton 形式挂到 header 右侧（快速切换）
- 每个字段 editingFinished / checkedChanged 触发即时保存，无需底部「保存配置」按钮
- 字体大小 / 字族由 _apply_runtime_ui_settings → apply_font_size_to_widget
  递归处理（保持与全站一致；不再在控件构造期 setFont，避免 pointSize/pixelSize
  错位导致的"字体未应用系统字号 / 密码字体过大"问题）
- 回显当前生效值（空输入=清除→回默认，对齐 websearch 旧卡语义）

注册方式：PluginManager 扫描 config_schema 后调
register_settings_card(..., make_card_class(plugin_name))，插件零 UI 代码。
"""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    ExpandSettingCard,
    FluentIcon,
    IndeterminateProgressBar,
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
    SpinBox,
    SwitchButton,
    TextEdit,
    isDarkTheme,
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


class _OptionPill(QLabel):
    """单个可点击选项胶囊：点击即选中（选中态 accent 高亮）"""

    clicked = Signal()

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)
        self.setAlignment(Qt.AlignCenter)
        self.refresh_style()

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.refresh_style()

    def refresh_style(self) -> None:
        from app.utils.design_tokens import Colors
        from app.utils.utils import get_font_family_css

        if self._selected:
            self.setStyleSheet(
                f"QLabel {{ color: {Colors.BUTTON_TEXT_ON_ACCENT}; background: {Colors.TEXT_ACCENT};"
                f" border: 1px solid {Colors.TEXT_ACCENT}; border-radius: 11px;"
                f" padding: 3px 14px; {get_font_family_css()} }}"
            )
        else:
            self.setStyleSheet(
                f"QLabel {{ color: {Colors.TEXT_SECONDARY}; background: transparent;"
                f" border: 1px solid {Colors.BORDER}; border-radius: 11px;"
                f" padding: 3px 14px; {get_font_family_css()} }}"
                f"QLabel:hover {{ color: {Colors.TEXT_PRIMARY}; border-color: {Colors.TEXT_ACCENT}; }}"
            )

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class SelectPillsRow(QWidget):
    """select 字段的分段选项行：一排可点击胶囊，点击即选（替代下拉框）

    API 与旧 ComboBox 用法对齐：currentData()/setCurrentData() 读写当前值，
    valueChanged 信号在用户点击切换时发射。
    """

    valueChanged = Signal(object)

    def __init__(self, options, parent=None):
        """options: List[(value, label)]，保持 schema 声明顺序"""
        super().__init__(parent)
        self._options = list(options or [])
        self._values = [v for v, _ in self._options]
        self._current = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._pills = {}
        for value, label in self._options:
            pill = _OptionPill(label, self)
            pill.clicked.connect(lambda v=value: self._on_pill_clicked(v))
            layout.addWidget(pill)
            self._pills[value] = pill
        layout.addStretch(1)
        # 初始无选中态由 _echo/_apply_value 统一回显

    def _on_pill_clicked(self, value) -> None:
        if value != self._current:
            self.setCurrentData(value)
            self.valueChanged.emit(value)

    def currentData(self):
        return self._current

    def setCurrentData(self, value) -> None:
        if value not in self._values and self._values:
            value = self._values[0]
        self._current = value
        for v, pill in self._pills.items():
            pill.set_selected(v == value)

    def refresh_style(self) -> None:
        for pill in self._pills.values():
            pill.refresh_style()


class _FieldRow(QWidget):
    """字段行：左侧 label，右侧输入控件。"""

    def __init__(self, label_text: str, control: QWidget, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(12)

        self._label = BodyLabel(label_text, self)
        self._label.setObjectName("fieldLabel")
        self._label.setMinimumWidth(120)
        layout.addWidget(self._label, 0, Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(control, 1, Qt.AlignRight)


class PluginConfigCard(ExpandSettingCard):
    """schema 驱动的折叠式插件配置卡（无 schema 时渲染为空，不报错）。

    继承 ExpandSettingCard 复用标题/折叠/header 样式；通过 viewLayout
    把行添加到内部 view；bool 字段挂到 header 右侧作为 SwitchButton。
    """

    def __init__(self, plugin_name: str, parent=None):
        self._plugin_name = plugin_name
        self._rows: Dict[str, QWidget] = {}  # key → 输入控件（兼容旧测试）
        self._bool_switches: Dict[str, SwitchButton] = {}  # key → switch 实例
        # 文本字段回显基线：记录最近一次 setText 的值，用于判断"内容是否真正变化"
        self._echoed: Dict[str, str] = {}
        # 回显/程序化设值期间置位，阻断信号触发 _on_field_changed（防循环写盘）
        self._echoing = False

        schema = PluginConfigRegistry.get_instance().get(self._plugin_name)
        title_text = schema.title if schema else "插件配置"
        # 副标题：字段数概览（无 schema 时给空串，避免占位）
        if schema and schema.fields:
            content_text = f"共 {len(schema.fields)} 项配置，点开展开"
        else:
            content_text = ""

        icon = self._resolve_plugin_icon() or FluentIcon.SETTING
        super().__init__(icon, title_text, content_text, parent)
        # viewLayout 边距收紧，让多行表单更紧凑
        self.viewLayout.setContentsMargins(48, 8, 48, 8)
        self.viewLayout.setSpacing(4)

        if schema is None:
            return

        for f in schema.fields:
            if f.type == "bool":
                switch = SwitchButton(self.card)
                self._bool_switches[f.key] = switch
                self._rows[f.key] = switch
                self.card.addWidget(switch)
                switch.setOnText(f.label)
                switch.setOffText(f.label)
                switch.checkedChanged.connect(lambda _checked, _k=f.key: self._on_field_changed(_k))
            elif f.type == "select":
                # 展开式分段选项：一排可点击胶囊替代下拉框（点击即选中保存）
                pills = SelectPillsRow(f.options, self.view)
                pills.valueChanged.connect(lambda _v, _k=f.key: self._on_field_changed(_k))
                row = _FieldRow(f.label, pills, self.view)
                self._rows[f.key] = pills
                self.viewLayout.addWidget(row)
            elif f.type == "number":
                spin = SpinBox(self.view)
                spin.setRange(f.min if f.min is not None else 0, f.max if f.max is not None else 2147483647)
                spin.setSingleStep(max(1, f.step))
                spin.valueChanged.connect(lambda *_a, _k=f.key: self._on_field_changed(_k))
                row = _FieldRow(f.label, spin, self.view)
                self._rows[f.key] = spin
                self.viewLayout.addWidget(row)
            elif f.type == "link":
                # 外链按钮：可点击超链接（系统浏览器打开），无存储值不参与回显
                link_text = f.placeholder or f.url
                link_label = BodyLabel(
                    f"<a href=\"{f.url}\">{link_text} ↗</a>", self.view
                )
                from app.utils.design_tokens import Colors

                link_label.setStyleSheet(f"QLabel {{ color: {Colors.TEXT_ACCENT}; }}")
                link_label.setOpenExternalLinks(True)
                row = _FieldRow(f.label, link_label, self.view)
                self.viewLayout.addWidget(row)
            elif f.type == "action":
                # 动作按钮：点击弹窗执行声明式工具编排（通用机制，见 ToolActionDialog），
                # 无存储值不参与保存/回显
                btn = PrimaryPushButton(f.placeholder or f.label, self.view)
                btn.clicked.connect(
                    lambda _c, _f=f, _p=self._plugin_name, _card=self: ToolActionDialog.open_for(
                        _card.view, _p, _f.label, _f.action, config_card=_card
                    )
                )
                row = _FieldRow(f.label, btn, self.view)
                self.viewLayout.addWidget(row)
            elif f.type == "textarea":
                edit = _PlainEdit(self.view)
                if f.placeholder:
                    edit.setPlaceholderText(f.placeholder)
                edit.setFixedHeight(max(2, f.rows) * 22 + 8)
                edit.editingFinished.connect(lambda _k=f.key: self._on_field_changed(_k))
                row = _FieldRow(f.label, edit, self.view)
                self._rows[f.key] = edit
                self.viewLayout.addWidget(row)
            else:
                edit = PasswordLineEdit() if f.type == "password" else LineEdit()
                edit.setClearButtonEnabled(True)
                if f.placeholder:
                    edit.setPlaceholderText(f.placeholder)
                row = _FieldRow(f.label, edit, self.view)
                self._rows[f.key] = edit
                edit.editingFinished.connect(lambda _k=f.key: self._on_field_changed(_k))
                self.viewLayout.addWidget(row)

        self._echo()

    def _resolve_plugin_icon(self):
        """读取插件自身 icon（按当前主题取 light/dark），失败回退 None。

        对齐 tab_panel._get_plugin_icon：经 PluginManager 拿 icon_config，
        主题图标缺失/读取失败时静默回退 FluentIcon.SETTING。
        """
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                return None
            plugin = pm.get_plugin(self._plugin_name)
            icon_config = getattr(plugin, "icon_config", None) if plugin else None
            if not icon_config:
                return None
            icon_path = icon_config.get("dark" if isDarkTheme() else "light")
            return QIcon(str(icon_path)) if icon_path else None
        except Exception:
            return None

    def _echo(self):
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
        elif f.type == "select":
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

    def _on_field_changed(self, key: str):
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
        elif target_field.type == "select":
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
            logger.warning(f"[PluginConfigCard] 网关启停失败 {self._plugin_name}: {e}")

    def _echo_field(self, key: str):
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


def make_card_class(plugin_name: str) -> type:
    """生成绑定 plugin_name 的无参构造卡片类（register_settings_card 的 widget_class 约定）"""

    class _BoundConfigCard(PluginConfigCard):
        def __init__(self, parent=None):
            super().__init__(plugin_name, parent)

    _BoundConfigCard.__name__ = f"PluginConfigCard[{plugin_name}]"
    return _BoundConfigCard


# ── action 字段：声明式工具编排弹窗（通用机制，无平台知识） ──────────────
#
# 消费 plugin.json 里 action 字段声明的 ToolActionSpec：
#   点击按钮 → 调注册工具(args) → image_data 弹窗展示图片 + content 作状态文案
#   → 声明了 poll 则按模板轮询（{data.<key>} 占位取上次结果）→ 命中 stop_when 终止。
# 宿主只实现这套通用编排；扫码登录等具体语义完全由插件 schema + 工具定义。


class _ToolActionWorker(QThread):
    """后台执行工具调用 + 轮询循环（同步 impl 跑在子线程，不阻塞 UI）"""

    result_ready = Signal(object)  # ToolResult（每轮：初始/轮询各发一次）
    finished_state = Signal(str, str)  # (终态, 说明)：done/error/stopped

    def __init__(self, tool: str, args: Dict[str, Any], poll, parent=None):
        super().__init__(parent)
        self._tool = tool
        self._args = dict(args or {})
        self._poll = poll  # ToolActionPoll | None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    # ── 工具调用 ──

    def _call_impl(self, kwargs: Dict[str, Any]) -> Any:
        """直接调注册工具 impl（设置卡无会话上下文，构造最小 tool_ctx）"""
        import inspect

        from app.tools.registry import ToolRegistry
        from app.tools.result import ToolResult

        reg = ToolRegistry.get_instance().get(self._tool)
        if reg is None:
            return ToolResult(False, error=f"工具未注册: {self._tool}")
        ctx = {"workdir": "", "session_id": "", "call_id": "", "env": {}, "services": {}}
        try:
            has_ctx = "tool_ctx" in inspect.signature(reg.impl).parameters
        except (TypeError, ValueError):
            has_ctx = True
        result = reg.impl(tool_ctx=ctx, **kwargs) if has_ctx else reg.impl(**kwargs)
        return result if isinstance(result, ToolResult) else ToolResult(True, content=str(result))

    # ── 模板与终止条件（通用） ──

    @staticmethod
    def _resolve_templates(value: Any, last_data: Dict[str, Any]) -> Any:
        """字符串值中 {data.<key>} 占位符 → 上次结果 data 字段值；未命中保留原样"""
        if not isinstance(value, str) or "{" not in value:
            return value

        def _sub(m: "re.Match") -> str:
            path = m.group(1)
            if path.startswith("data.") and path[5:] in last_data:
                return str(last_data[path[5:]])
            return m.group(0)

        return re.sub(r"\{([a-zA-Z0-9_.]+)\}", _sub, value)

    @staticmethod
    def _match_stop(stop_when: Dict[str, Any], result) -> Optional[str]:
        """终止条件命中检查：路径 data.<key> / error；返回命中描述或 None"""
        rd = getattr(result, "data", None) or {}
        for path, hits in (stop_when or {}).items():
            hit_set = {str(h) for h in hits}
            if path == "error":
                if not result.success and result.error:
                    return f"error={result.error[:120]}"
            elif path.startswith("data.") and path[5:] in rd:
                if str(rd[path[5:]]) in hit_set:
                    return f"{path}={rd[path[5:]]}"
        return None

    # ── 主循环 ──

    def run(self) -> None:  # noqa: C901
        import time

        from app.tools.result import ToolResult

        last_data: Dict[str, Any] = {}
        result = self._call_impl(self._args)
        self.result_ready.emit(result)
        if not isinstance(result, ToolResult):
            self.finished_state.emit("error", "工具返回非法结果")
            return
        if not result.success:
            self.finished_state.emit("error", str(result.error or "调用失败"))
            return
        last_data = result.data or {}

        if self._poll is None:
            self.finished_state.emit("done", "执行完成")
            return

        for _ in range(self._poll.max_rounds):
            if self._cancelled:
                self.finished_state.emit("stopped", "已取消")
                return
            time.sleep(self._poll.interval_ms / 1000.0)
            if self._cancelled:
                self.finished_state.emit("stopped", "已取消")
                return

            kwargs = {
                k: self._resolve_templates(v, last_data)
                for k, v in (self._poll.args or {}).items()
            }
            result = self._call_impl(kwargs)
            if not isinstance(result, ToolResult):
                self.finished_state.emit("error", "工具返回非法结果")
                return
            self.result_ready.emit(result)
            last_data = result.data or last_data if result.success else last_data

            hit = self._match_stop(self._poll.stop_when, result)
            if hit:
                self.finished_state.emit(
                    "done" if result.success else "error",
                    f"终止: {hit}" + ("" if result.success else f" | {str(result.error or '')[:120]}"),
                )
                return
            # 未命中终止条件：单轮失败（网络抖动/长轮询超时）不终止，继续下一轮；
            # 仅连续失败达 max_rounds 才止，保证扫码确认这类长等待不丢

        self.finished_state.emit("stopped", f"达最大轮次 {self._poll.max_rounds}")


class ToolActionDialog(QDialog):
    """通用工具动作弹窗：图片（image_data）+ 状态文案 + 轮询进度。

    类方法 open_for(parent, plugin_name, title, spec) 一键拉起。
    """

    _instances = []  # 防重复点击开出多窗

    def __init__(self, plugin_name: str, title: str, spec, parent=None, *, config_card=None):
        super().__init__(parent)
        self._config_card = config_card  # 发起动作的设置卡（完成后回显刷新）
        self.setWindowTitle(f"{title} - {plugin_name}")
        self.setModal(True)
        self.setMinimumWidth(spec.dialog_width)
        if spec.dialog_height:
            self.setMinimumHeight(spec.dialog_height)
        self._image_width = spec.image_width
        self._worker: Optional[_ToolActionWorker] = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self._image_label = QLabel(self)
        self._image_label.setAlignment(Qt.AlignCenter)
        self._image_label.setMinimumHeight(80)
        self._image_label.hide()
        layout.addWidget(self._image_label)

        self._status_label = BodyLabel("正在执行…", self)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._progress = IndeterminateProgressBar(self)
        self._progress.start()
        layout.addWidget(self._progress)

        from qfluentwidgets import PushButton

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._close_btn = PushButton("关闭", self)
        self._close_btn.clicked.connect(self._on_close)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

        # 启动 worker
        self._worker = _ToolActionWorker(spec.tool, spec.args, spec.poll, self)
        self._worker.result_ready.connect(self._on_result)
        self._worker.finished_state.connect(self._on_finished)
        self._worker.start()

    # ── 事件 ──

    def _on_result(self, result) -> None:
        """每轮结果：图片刷新展示 + content 刷新状态文案"""
        img = getattr(result, "image_data", None)
        if img and img.get("data"):
            try:
                pm = QPixmap()
                if pm.loadFromData(base64.b64decode(img["data"])):
                    self._image_label.setPixmap(
                        pm.scaledToWidth(self._image_width, Qt.SmoothTransformation)
                    )
                    self._image_label.show()
                    self.adjustSize()
            except Exception:
                pass
        content = getattr(result, "content", None)
        if content:
            self._status_label.setText(str(content))

    def _on_finished(self, state: str, message: str) -> None:
        self._progress.stop()
        self._progress.hide()
        self._status_label.setText(f"[{ {'done': '完成', 'error': '失败', 'stopped': '中止'}.get(state, state) }] {message}")
        # 工具可能已写配置存储（如登录 token），回显刷新发起动作的设置卡
        try:
            if self._config_card is not None:
                self._config_card._echo()
        except Exception:
            pass
        if state == "error":
            from qfluentwidgets import InfoBar, InfoBarPosition

            InfoBar.error("执行失败", message[:160], parent=self, position=InfoBarPosition.TOP, duration=4000)
        elif state == "done":
            from qfluentwidgets import InfoBar, InfoBarPosition

            InfoBar.success("执行完成", message[:160], parent=self, position=InfoBarPosition.TOP, duration=4000)

    def _on_close(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        self.accept()

    # ── 入口 ──

    @classmethod
    def open_for(cls, parent, plugin_name: str, title: str, spec, *, config_card=None) -> None:
        """打开动作弹窗（同一时刻只保留一个实例）"""
        for dlg in cls._instances:
            if dlg.isVisible():
                dlg.raise_()
                return
        dlg = cls(plugin_name, title, spec, parent, config_card=config_card)
        cls._instances.append(dlg)
        dlg.finished.connect(lambda *_: cls._instances.remove(dlg) if dlg in cls._instances else None)
        dlg.show()
