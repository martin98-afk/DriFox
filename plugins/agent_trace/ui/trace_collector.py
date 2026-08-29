# -*- coding: utf-8 -*-
"""agent_trace.TraceCollector — 后台轨迹采集器。

职责：
1. 把当前会话的 ``session.messages`` 1-to-1 投影成 ``TraceRecord`` 列表。
2. 订阅 ``ChatBackend`` 信号，给 in-flight 调用补齐开始/结束时间戳。
3. 监听 session 切换 / 新建会话 → 清空轨迹。

设计原则（不修改 messages，不修改 signals）：
- 采集器**只读** sessions.messages，不注入、不删除。
- 实时信号仅用于精度调整（in-flight 起止）。
- 信号连接在 ``attach(backend, main_widget)`` 时建立，``detach()`` 时断开。
"""

from __future__ import annotations

import json
import time
import traceback
from typing import Any, Dict, List, Optional

from loguru import logger
from PyQt5.QtCore import QObject, pyqtSignal

from .trace_models import (
    EntryKind,
    TraceRecord,
    content_to_text,
    infer_message_kind,
    message_label,
    message_source,
    truncate,
)


class TraceCollector(QObject):
    """常驻 Qt 对象，订阅后端信号并维护当前会话轨迹快照。"""

    # 列表刷新（增删 entry）— 增量模式（新增段尾）
    recordsAppended = pyqtSignal(int)  # 起始 index
    # 单条记录 in-flight/end 状态翻转
    recordUpdated = pyqtSignal(int)  # entry index
    # 整体清空（session 切换）
    recordsReset = pyqtSignal()

    def __init__(self, parent: QObject = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        # session.messages 长度快照（增量 sync 用）
        self._last_session_messages_len: int = 0
        # 区分 in-flight 的工具调用：tool_call_id → TraceRecord index
        self._pending_tool_records: Dict[str, int] = {}
        # 当前 in-flight 的 assistant stream：TraceRecord index 或 None
        self._active_assistant_idx: Optional[int] = None
        # 当前 session_id（用于判断会话切换）
        self._active_session_id: str = ""
        # 当前正在处理的 hook 事件：event_name → TraceRecord index
        self._active_hook_records: Dict[str, int] = {}
        # 已绑定的 backend（用于 detach）
        self._bound_backend: Optional[Any] = None
        self._bound_main_widget: Optional[Any] = None

    # ──────────────────── 公共 API ────────────────────

    def attach(self, backend: Any, main_widget: Any) -> None:
        """把采集器挂到指定 backend / 主窗口。

        必须由主窗口在 ``backend.initialize`` 完成之后、``messages_updated``
        首次到达之前调用（注册信号回调 + 拉首条 session 同步）。
        """
        if backend is None:
            return
        self.detach()  # 幂等：先拆再装
        self._bound_backend = backend
        self._bound_main_widget = main_widget

        try:
            backend.tool_call_started.connect(self._on_tool_call_started)
            backend.tool_result_received.connect(self._on_tool_result_received)
            backend.hook_status_changed.connect(self._on_hook_status_changed)
            backend.stream_finished.connect(self._on_stream_finished)
            # 主程序无独立的 ``messages_updated`` pyqtSignal；
            # ``_hook_messages_updated`` 是主程序在所有 hook 注入消息 / 工具结果
            # 写入 session.messages 后同步触发的 cross-thread-safe 信号 — 当作
            # 「消息列表已就绪」节拍，每拍做一次增量同步即可保证轨迹不会漏消息。
            if hasattr(backend, "_hook_messages_updated"):
                backend._hook_messages_updated.connect(self._on_messages_updated)
        except Exception as e:
            logger.warning(f"[agent_trace] attach signals failed: {e}")
        # 首条同步（拉取当前已存在的 messages）
        self._sync_session_state(emit_reset=True)

    def detach(self) -> None:
        """解除所有 signal 连接。"""
        if self._bound_backend is not None:
            try:
                self._bound_backend.tool_call_started.disconnect(self._on_tool_call_started)
                self._bound_backend.tool_result_received.disconnect(self._on_tool_result_received)
                self._bound_backend.hook_status_changed.disconnect(self._on_hook_status_changed)
                self._bound_backend.stream_finished.disconnect(self._on_stream_finished)
                if hasattr(self._bound_backend, "_hook_messages_updated"):
                    self._bound_backend._hook_messages_updated.disconnect(self._on_messages_updated)
            except TypeError, RuntimeError:
                pass  # 未连接 / 已析构
        self._bound_backend = None
        self._bound_main_widget = None

    @property
    def records(self) -> List[TraceRecord]:
        return list(self._records)

    # ──────────────────── 会话同步 ────────────────────

    def _current_session(self) -> Optional[Any]:
        """获取 backend 当前会话（可能为 None）。"""
        be = self._bound_backend
        if be is None:
            return None
        try:
            return be.get_current_session()
        except Exception:
            return None

    def _sync_session_state(self, emit_reset: bool = False) -> None:
        """全量重同步当前 session 的 messages。"""
        session = self._current_session()
        if session is None:
            if self._records:
                self._records.clear()
                self._last_session_messages_len = 0
                self._active_session_id = ""
                self._recordsReset.emit()
            return

        sid = getattr(session, "session_id", "")
        if sid and sid != self._active_session_id:
            self._active_session_id = sid
            self._records.clear()
            self._last_session_messages_len = 0
            self._pending_tool_records.clear()
            self._active_assistant_idx = None
            self._active_hook_records.clear()
            emit_reset = True

        messages = getattr(session, "messages", None) or []
        if not messages:
            if emit_reset and not self._records:
                self.recordsReset.emit()
            return

        # 增量追加
        start_idx = len(self._records)
        now = time.time()
        appended: List[TraceRecord] = []
        for offset, msg in enumerate(messages[self._last_session_messages_len :]):
            record = self._record_from_message(
                msg,
                idx=self._last_session_messages_len + offset,
                now=now,
            )
            appended.append(record)
        if appended:
            self._records.extend(appended)
            self.recordsAppended.emit(start_idx)
        self._last_session_messages_len = len(messages)

        if emit_reset and start_idx == 0 and not appended:
            self.recordsReset.emit()

    def _on_messages_updated(self, *_args: Any) -> None:
        """backend 推送 messages 更新 — 增量同步。"""
        try:
            self._sync_session_state(emit_reset=False)
        except Exception as e:
            logger.warning(f"[agent_trace] _sync_session_state failed: {e}\n{traceback.format_exc()}")

    def _record_from_message(self, msg: Dict[str, Any], idx: int, now: float) -> TraceRecord:
        """从一条 session.message dict 派生 TraceRecord。"""
        kind = infer_message_kind(msg)
        label = message_label(msg)
        raw_text = content_to_text(msg.get("content"))
        preview = truncate(raw_text, 140)
        source = message_source(idx)

        # 工具结果记录额外标记成功/失败
        meta: Dict[str, Any] = {}
        if kind == EntryKind.TOOL:
            tool_call_id = msg.get("tool_call_id") or ""
            if tool_call_id:
                meta["tool_call_id"] = tool_call_id
            meta["name"] = msg.get("name") or msg.get("tool_name") or "tool"

        is_pending = False
        is_error = False
        # tool_calls（含在 assistant message 中）— 单独的工具调用条目由实时信号负责
        if kind == EntryKind.ASSISTANT and msg.get("tool_calls"):
            meta["has_tool_calls"] = True

        timestamp_str = msg.get("timestamp")
        start_ts = self._parse_timestamp(timestamp_str) or now
        # session.messages 中的 hook 注入消息和真实 user 消息间隔通常很短，
        # end_ts 暂留 0，in-flight 时由 stream_finished 补齐。
        return TraceRecord(
            kind=kind,
            label=label,
            preview=preview,
            raw=raw_text,
            source=source,
            start_ts=start_ts,
            end_ts=0.0,
            is_pending=is_pending,
            is_error=is_error,
            meta=meta,
        )

    @staticmethod
    def _parse_timestamp(s: str) -> Optional[float]:
        """把 'YYYY-MM-DD HH:MM:SS' 解析为 epoch（浮点秒）。失败返回 None。"""
        if not s or not isinstance(s, str):
            return None
        import datetime as _dt

        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = _dt.datetime.strptime(s, fmt)
                return dt.timestamp()
            except ValueError:
                continue
        return None

    # ──────────────────── 实时信号回调 ────────────────────

    def _on_tool_call_started(self, tool_call_id: str, tool_name: str, arguments: Dict[str, Any]) -> None:
        """工具调用开始 — 创建 in-flight TOOL 记录。"""
        if not tool_call_id:
            return
        # 去重
        if tool_call_id in self._pending_tool_records:
            return
        preview_args = json.dumps(arguments, ensure_ascii=False)[:140] if arguments else ""
        record = TraceRecord(
            kind=EntryKind.TOOL,
            label=tool_name or "tool",
            preview=preview_args or "（调用中…）",
            raw=json.dumps(
                {"tool_call_id": tool_call_id, "name": tool_name, "arguments": arguments}, ensure_ascii=False, indent=2
            ),
            source=f"tool_call_started · {tool_name}",
            start_ts=time.time(),
            end_ts=0.0,
            is_pending=True,
            is_error=False,
            meta={"tool_call_id": tool_call_id, "name": tool_name},
        )
        self._records.append(record)
        idx = len(self._records) - 1
        self._pending_tool_records[tool_call_id] = idx
        self.recordsAppended.emit(idx)

    def _on_tool_result_received(self, tool_call_id: str, name: str, result: Any, success: bool) -> None:
        """工具调用结果 — 标记对应 in-flight 记录结束。"""
        idx = self._pending_tool_records.pop(tool_call_id, None)
        if idx is None or idx >= len(self._records):
            # 没找到对应 pending：作为一条新 TOOL 记录加入（结果先于调用信号到达的兜底）
            if not tool_call_id:
                return
            record = TraceRecord(
                kind=EntryKind.TOOL,
                label=name or "tool",
                preview=truncate(content_to_text(result), 140),
                raw=content_to_text(result),
                source=f"tool_result_received · {name}",
                start_ts=time.time(),
                end_ts=time.time(),
                is_pending=False,
                is_error=not success,
                meta={"tool_call_id": tool_call_id, "name": name},
            )
            self._records.append(record)
            self.recordsAppended.emit(len(self._records) - 1)
            return
        rec = self._records[idx]
        rec.end_ts = time.time()
        rec.is_pending = False
        rec.is_error = not success
        # 若结果存在且记录 preview 还是占位，覆盖之
        if result is not None:
            text = content_to_text(result)
            rec.preview = truncate(text, 140)
            rec.raw = text
        self.recordUpdated.emit(idx)

    def _on_hook_status_changed(self, event_name: str, status_message: str, is_start: bool) -> None:
        """Hook 状态变化 — 引入临时 CONTEXT 条目（生命周期独立于 session.messages）。

        仅用于右侧面板的「模型执行 → hook 注入」时间线展示。
        """
        if not event_name:
            return
        if is_start:
            label = event_name
            preview = status_message or "Hook 注入中…"
            record = TraceRecord(
                kind=EntryKind.CONTEXT,
                label=label,
                preview=preview,
                raw=status_message or "",
                source=f"hook · {event_name}",
                start_ts=time.time(),
                end_ts=0.0,
                is_pending=True,
                is_error=False,
                meta={"hook_event": event_name},
            )
            self._records.append(record)
            idx = len(self._records) - 1
            self._active_hook_records[event_name] = idx
            self.recordsAppended.emit(idx)
        else:
            idx = self._active_hook_records.pop(event_name, None)
            if idx is not None and idx < len(self._records):
                rec = self._records[idx]
                rec.end_ts = time.time()
                rec.is_pending = False
                self.recordUpdated.emit(idx)

    def _on_stream_finished(self, _payload: Dict[str, Any]) -> None:
        """一个 assistant turn 流结束 — 标记所有仍 in-flight 的辅助记录收尾。"""
        # 兜底：任何仍 is_pending 的记录盖上 end_ts（避免「永久 in-flight」）
        now = time.time()
        for i, rec in enumerate(self._records):
            if rec.is_pending and rec.end_ts <= 0:
                rec.end_ts = now
                rec.is_pending = False
                self.recordUpdated.emit(i)
        self._active_assistant_idx = None
