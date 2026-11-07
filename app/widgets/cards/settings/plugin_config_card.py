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

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    ExpandSettingCard,
    FluentIcon,
    IndeterminateProgressBar,
    isDarkTheme,
)

from loguru import logger

from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
from app.widgets.cards.settings.expand_height_mixin import DynamicHeightExpandCardMixin


from app.widgets.cards.settings.plugin_config_form import (  # noqa: F401 - 兼容旧导入路径
    PluginConfigForm,
    SelectComboRow,
    _FieldRow,
    _PlainEdit,
)


class PluginConfigCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
    """schema 驱动的折叠式插件配置卡（表单实现见 PluginConfigForm）。

    标题 = schema.title；副标题 = 插件归属 + 字段数 + 配置状态（待填 N 项 / 未配置）。
    字段渲染、回显、即时保存全部委托给 PluginConfigForm，与插件详情页共用一套逻辑。
    """

    def __init__(self, plugin_name: str, parent=None):
        self._plugin_name = plugin_name
        schema = PluginConfigRegistry.get_instance().get(plugin_name)
        title_text = schema.title if schema else "插件配置"
        # 副标题：归属 + 字段数（不同插件的 schema.title 可能重名，归属是扫视关键）
        plugin_label = self._plugin_display_name()
        if schema and schema.fields:
            content_text = f"{plugin_label} · {len(schema.fields)} 项配置"
        else:
            content_text = plugin_label
        self._base_content_text = content_text

        icon = self._resolve_plugin_icon() or FluentIcon.SETTING
        super().__init__(icon, title_text, content_text, parent)
        # viewLayout 边距收紧，让多行表单更紧凑
        self.viewLayout.setContentsMargins(48, 8, 48, 8)
        self.viewLayout.setSpacing(4)

        self._form = PluginConfigForm(plugin_name, self.view)
        self.viewLayout.addWidget(self._form)
        self._rows = self._form._rows  # 兼容既有测试/调用方
        self._refresh_header_hint()

    def _resync_height(self) -> None:
        """内容尺寸变了（如回显后长文本换行数变化）后重新定格高度。"""
        try:
            self._adjust_view_size()
        except Exception as e:  # noqa: BLE001 — 高度校正失败不影响交互
            logger.debug(f"[PluginConfigCard] 高度同步跳过: {e}")

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

    def _plugin_display_name(self) -> str:
        """插件显示名：manifest display_name → name → 目录名

        卡片标题是配置块名（schema.title），归属只能靠副标题表达；插件名缺失时
        回退目录名，保证任何情况下副标题都有内容。
        """
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if pm.is_initialized():
                plugin = pm.get_plugin(self._plugin_name)
                if plugin is not None:
                    name = str(plugin.manifest.get("display_name") or plugin.name or "").strip()
                    if name:
                        return name
        except Exception:
            pass
        return self._plugin_name

    def _refresh_header_hint(self) -> None:
        """副标题追加配置状态（待填 N 项 / 未配置），无状态时保持归属文案"""
        text = getattr(self, "_base_content_text", "")
        state, missing = self.config_status()
        if state == "missing":
            hint = f"待填 {len(missing)} 项"
        elif state == "unconfigured":
            hint = "未配置"
        else:
            hint = ""
        if hint:
            text = f"{text} · {hint}" if text else hint
        label = getattr(self, "contentLabel", None)
        if label is not None and text:
            label.setText(text)

    def _echo(self):
        """回显当前生效值（表单实现，供 action 弹窗完成后刷新使用）"""
        self._form.echo()
        self._refresh_header_hint()

    def config_status(self):
        """配置完整度判定（委托表单，与详情页同一口径）"""
        return self._form.config_status()

    def matches(self, keyword: str) -> bool:
        """搜索命中（委托表单：插件名 / 配置块标题 / 字段 label、key、说明）"""
        return self._form.matches(keyword)

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

    result_ready = pyqtSignal(object)  # ToolResult（每轮：初始/轮询各发一次）
    finished_state = pyqtSignal(str, str)  # (终态, 说明)：done/error/stopped

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
