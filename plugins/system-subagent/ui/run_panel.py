# -*- coding: utf-8 -*-
"""子智能体运行面板（消息卡内嵌面板形态）

运行区的唯一形态：挂在 assistant 气泡末尾（正文/任务看板/成果栏之后、页脚之前），
与任务看板同位置、同风格。每个子智能体一行（旋转图标 + agent 名 + 任务描述），
行内展开看详情，保留「进入会话」「停止」按钮。

与旧浮动卡（``app/widgets/cards/floating/sub_agent_compact_widget.py``）的差异：

1. 无卡片外壳/关闭按钮/自动隐藏，跟随宿主卡片存活；无任务整区隐藏。
2. 行区不做限高内滚（同任务看板取舍）：QScrollArea 的 sizeHint 仅在 resize 时
   刷新，动态增删行后返回滞后值，宿主高度链会拿到错误面板高度（裁切 + 滚动上界虚抬）。
3. 全部完成后自动折叠成一行摘要（可再展开回看），不消失、不打断阅读。

数据入口全部是显式方法（宿主/桥接层直推），高度变化经 ``heightChanged`` 上报
（契约同 ``InlineTodoPanel``：必须报实际占高，宿主据此做外层滚动锚定补偿）。
"""

from typing import Any, Dict, List, Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import TransparentToolButton

from app.utils.motion import LoopTimer
from app.utils.utils import get_icon
from app.widgets._workbench_helpers import _SectionHeader

from .agent_row import AgentTaskRow

_ROTATION_TICK_MS = 60


class SubAgentRunPanel(QWidget):
    """子智能体运行态面板：一行一任务，全部完成后折叠收起。"""

    # 面板可见高度变化 → 宿主（MessageCard）重算卡片高度与外层锚定
    heightChanged = Signal()
    # 行内「进入会话」（task_id, agent_name）
    enterSessionRequested = Signal(str, str)
    # 行内「停止」（task_id）
    stopRequested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._rows: Dict[str, AgentTaskRow] = {}
        self._collapsed = False
        self._rotation_angle = 0

        # 旋转与耗时都以「有运行中行且面板可见」为门控：不可见时零回调。
        self._rotation_timer = LoopTimer(self, _ROTATION_TICK_MS, self._tick_rotation, gate=self._needs_tick)
        self._time_timer = LoopTimer(self, 1000, self._tick_elapsed, gate=self._needs_tick)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)

        # ── 头部：图标 + 标题 + 统计 + 折叠按钮 ──
        self._header = _SectionHeader("子智能体", "设置-subagent", self)
        self._collapse_btn = TransparentToolButton(get_icon("展开"), self)
        self._collapse_btn.setFixedSize(22, 22)
        self._collapse_btn.setToolTip("展开子智能体列表")
        self._collapse_btn.clicked.connect(self._on_collapse_clicked)
        hdr_layout = self._header.layout()
        hdr_layout.insertWidget(hdr_layout.count(), self._collapse_btn)
        self._header.installEventFilter(self)
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.setToolTip("点击展开子智能体列表")
        layout.addWidget(self._header)

        # ── 行容器（展开态可见，全量展示不限高）──
        self._body_wrap = QWidget(self)
        self._body_wrap.setObjectName("subAgentRunBody")
        self._body_layout = QVBoxLayout(self._body_wrap)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(4)
        layout.addWidget(self._body_wrap)

        self.hide()  # 无任务整区隐藏

    # ── 公共查询 ──

    def has_task(self, task_id: str) -> bool:
        """该任务是否已有行（补历史数据去重用）。"""
        return task_id in self._rows

    def task_ids(self) -> List[str]:
        return list(self._rows.keys())

    def is_collapsed(self) -> bool:
        return self._collapsed

    # ── 数据入口 ──

    def add_task(self, task_id: str, agent_name: str, task_description: str, model_name: str = "") -> None:
        """添加运行中任务行；已存在则只补模型名（保留已有统计，重开不重置）。"""
        existing = self._rows.get(task_id)
        if existing is not None:
            if model_name:
                existing.set_model_name(model_name)
            self._emit_height()
            return

        row = AgentTaskRow(task_id, agent_name, task_description, model_name, self._body_wrap)
        row.toggled.connect(self._on_row_toggled)
        row.enter_session_requested.connect(self._on_row_enter_session)
        row.stop_requested.connect(self._on_row_stop)
        self._rows[task_id] = row
        self._body_layout.addWidget(row)

        ctx_info = self._resolve_context_info(model_name)
        if ctx_info:
            row.set_context_info(ctx_info)

        self._ensure_expanded()
        self._sync_visibility()
        self._update_stats()
        self._start_ticks()
        self._emit_height()

    def finish_task(self, task_id: str, success: bool = True) -> None:
        """标记任务完成；全部完成时自动折叠（行保留，可再展开回看）。"""
        row = self._rows.get(task_id)
        if row is None:
            return
        row.finish(success)
        self._update_stats()
        if all(not r.is_running for r in self._rows.values()):
            self._set_collapsed(True)
        self._emit_height()

    def add_tool_call(self, task_id: str, tool_name: str = "", args: Optional[dict] = None) -> None:
        """记录一次工具调用（只推进计数，工具名/参数在详情里不展示）。"""
        row = self._rows.get(task_id)
        if row is not None:
            row.increment_tool_count()

    def set_task_tool_count(self, task_id: str, count: int) -> None:
        """直接设定工具调用次数（迁卡后按桥里的累计值对齐，不做增量累加）。"""
        row = self._rows.get(task_id)
        if row is not None:
            row.set_tool_count(count)

    def set_task_context(self, task_id: str, info: str) -> None:
        """设置任务的上下文用量文字（如 ``12.5K tokens``）。"""
        row = self._rows.get(task_id)
        if row is not None:
            row.set_context_info(info)

    def set_task_model(self, task_id: str, model_name: str) -> None:
        """运行中补设模型名（派发时还拿不到配置的场景）。"""
        row = self._rows.get(task_id)
        if row is not None and model_name:
            row.set_model_name(model_name)

    def remove_task(self, task_id: str) -> None:
        """移除单行（新批次清理已完成任务时用）。"""
        row = self._rows.pop(task_id, None)
        if row is None:
            return
        self._body_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        self._sync_visibility()
        self._update_stats()
        self._emit_height()

    def clear(self) -> None:
        """清空全部行并隐藏面板（会话切换 / 插件卸载）。"""
        for row in self._rows.values():
            self._body_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._rotation_angle = 0
        self._stop_ticks()
        self._update_stats()
        self._sync_visibility()
        self._emit_height()

    # ── 历史回填（会话重载）──

    def restore_items(self, items: List[Dict[str, Any]]) -> None:
        """按历史条目重建已完成行（会话重载路径，幂等）。

        行数据来自工具结果落库的 task_ids（插件 loader 查表整理成 item），
        与运行态投影（桥接层 ``add_task``）互不重叠：本方法只补已完成行。

        Args:
            items: [{task_id, agent_name, description, model_name?, tool_call_count?,
                     elapsed_seconds?, context_usage?, success?}, ...]
        """
        restored = False
        for item in items or []:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("task_id") or "")
            if not task_id or task_id in self._rows:
                continue
            self.show_completed_task(
                task_id,
                str(item.get("agent_name") or ""),
                str(item.get("description") or ""),
                model_name=str(item.get("model_name") or ""),
                tool_call_count=int(item.get("tool_call_count") or 0),
                elapsed_seconds=int(item.get("elapsed_seconds") or 0),
                context_usage=str(item.get("context_usage") or ""),
                success=bool(item.get("success", True)),
            )
            restored = True
        if restored and not self._needs_tick():
            # 历史卡默认折叠（同成果栏）：几十张历史卡各撑开一列已完成行会淹没正文
            self._set_collapsed(True)
            self._emit_height()

    def show_completed_task(
        self,
        task_id: str,
        agent_name: str,
        task_description: str,
        model_name: str = "",
        tool_call_count: int = 0,
        elapsed_seconds: int = 0,
        context_usage: str = "",
        success: bool = True,
    ) -> None:
        """补一条已完成任务行（会话重载 / 历史回看）。

        Args:
            success: 是否成功完成（失败/中止的行显示 ❌）
        """
        if task_id in self._rows:
            return
        import time as _time

        row = AgentTaskRow(task_id, agent_name, task_description, model_name, self._body_wrap)
        row.toggled.connect(self._on_row_toggled)
        row.enter_session_requested.connect(self._on_row_enter_session)
        self._rows[task_id] = row
        self._body_layout.addWidget(row)

        for _ in range(max(0, int(tool_call_count))):
            row.increment_tool_count()
        if elapsed_seconds > 0:
            row._start_time = _time.time() - elapsed_seconds
            mins, secs = divmod(int(elapsed_seconds), 60)
            row.time_label.setText(f"⏱{mins:02d}:{secs:02d}")
            if row._elapsed_label_detail is not None:
                row._elapsed_label_detail.setText(f"{mins:02d}:{secs:02d}")
        if model_name:
            row.set_model_name(model_name)
        if context_usage:
            row.set_context_info(context_usage)
        row.finish(success=success)

        self._ensure_expanded()
        self._sync_visibility()
        self._update_stats()
        self._emit_height()

    # ── 视觉刷新 ──

    def refresh_style(self) -> None:
        """主题切换：头部与所有行重放固定 QSS（宿主在主题切换时调用）。"""
        try:
            self._header.refresh_style()
            for row in self._rows.values():
                row.refresh_row_style()
        except RuntimeError:
            pass

    # ── 内部：折叠 / 可见性 / 统计 ──

    def eventFilter(self, obj, event) -> bool:
        """header 整行左键点击 = 折叠/展开（按钮自身消费点击，不冒泡到这）"""
        if obj is self._header and event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            self._on_collapse_clicked()
            return True
        return super().eventFilter(obj, event)

    def _on_collapse_clicked(self) -> None:
        self._set_collapsed(not self._collapsed)
        self._emit_height()

    def _set_collapsed(self, collapsed: bool) -> None:
        """设置折叠态（不 emit，由调用方决定何时上报高度）。"""
        self._collapsed = collapsed
        self._body_wrap.setVisible(not collapsed)
        self._collapse_btn.setIcon(get_icon("展开" if collapsed else "折叠"))
        self._collapse_btn.setToolTip("展开子智能体列表" if collapsed else "折叠子智能体列表")
        self._header.setToolTip("点击展开子智能体列表" if collapsed else "点击折叠子智能体列表")

    def _ensure_expanded(self) -> None:
        """有任务在跑就展开（新批次到达不沿用上一批的折叠意愿）。"""
        if self._collapsed:
            self._set_collapsed(False)

    def _sync_visibility(self) -> None:
        """有行显示、无行隐藏（隐藏后占用高度归零，宿主锚定按 0 记账）。"""
        self.setVisible(bool(self._rows))

    def _update_stats(self) -> None:
        running = sum(1 for r in self._rows.values() if r.is_running)
        total = len(self._rows)
        if running > 0:
            self._header.set_extra(f"{running} 个执行中 / {total} 个任务")
        elif total > 0:
            self._header.set_extra(f"全部完成 · {total} 个任务")
        else:
            self._header.set_extra("")

    def _emit_height(self) -> None:
        """上报高度变化：先清 QBoxLayout 的 sizeHint 缓存，宿主读到的才是新值。"""
        try:
            lay = self.layout()
            if lay is not None:
                lay.invalidate()
            self.updateGeometry()
            self.heightChanged.emit()
        except RuntimeError:
            pass

    # ── 内部：动画节拍 ──

    def _needs_tick(self) -> bool:
        return any(r.is_running for r in self._rows.values())

    def _start_ticks(self) -> None:
        if not self._rotation_timer.running:
            self._rotation_timer.start()
        if not self._time_timer.running:
            self._time_timer.start()

    def _stop_ticks(self) -> None:
        self._rotation_timer.stop()
        self._time_timer.stop()

    def _tick_rotation(self) -> None:
        if not self._needs_tick():
            return
        self._rotation_angle = (self._rotation_angle + 12) % 360
        for row in self._rows.values():
            row.set_rotation_angle(self._rotation_angle)

    def _tick_elapsed(self) -> None:
        for row in self._rows.values():
            row.update_elapsed()

    # ── 内部：行信号 ──

    def _on_row_toggled(self, task_id: str = "") -> None:
        self._emit_height()

    def _on_row_enter_session(self, task_id: str) -> None:
        row = self._rows.get(task_id)
        self.enterSessionRequested.emit(task_id, row.agent_name if row else "")

    def _on_row_stop(self, task_id: str) -> None:
        self.stopRequested.emit(task_id)

    @staticmethod
    def _resolve_context_info(model_name: str) -> str:
        """从模型名推断上下文窗口文字（与旧浮动卡同口径）。"""
        if not model_name:
            return ""
        try:
            from app.core.modelmeta.model_capabilities import get_model_capabilities

            caps = get_model_capabilities(model_name)
            ctx_limit = caps.get("context_limit", 0) or caps.get("max_context_tokens", 0) or 0
            if ctx_limit:
                if ctx_limit >= 1000000:
                    return f"{ctx_limit // 1000000}M 上下文"
                if ctx_limit >= 1000:
                    return f"{ctx_limit // 1000}K 上下文"
                return f"{ctx_limit} 上下文"
        except Exception:
            pass
        name_lower = model_name.lower()
        if "128k" in name_lower or "claude-3" in name_lower:
            return "200K 上下文"
        if "gemini" in name_lower:
            return "1M 上下文"
        if "gpt-4" in name_lower or "gpt4" in name_lower:
            return "128K 上下文"
        return ""

    def showEvent(self, event) -> None:
        """可见性恢复时补报一次高度（挂载/切卡导致的占高变化没有数据路径可 emit）"""
        super().showEvent(event)
        self._start_ticks()
        self._emit_height()
