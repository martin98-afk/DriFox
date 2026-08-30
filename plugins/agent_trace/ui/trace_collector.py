# -*- coding: utf-8 -*-
"""agent_trace.TraceCollector — 后台轨迹采集器（v3）。

核心设计（在 v2「messages 唯一事实源」基础上强化）：

1. **session.messages 是唯一事实源**：records 是 messages 的 1:1 稳定投影。
2. **实时信号只写 timing 表**：tool/stream 起止时间记到 ``_timing`` /
   ``_streams``，投影时按 tool_call_id / assistant 序号回填精确毫秒时长。
3. **in-flight 尾巴**放 ``_tail`` 单独展示，落盘后自动被正式记录取代。
4. **tail 稳定化**：``_set_tail`` 内部比对签名，内容没变就**不发信号** —
   修复 v2 每次 ``_sync`` 无条件 emit tailChanged 导致列表全量重建
   （「历史记录一直在刷新」）的问题。
5. **TraceCollectorHub**：per-window 常驻采集器管理。每个对话标签页的
   backend 各挂一个 collector **持续收集**（后台标签页的工具耗时/流耗时不
   丢失），轨迹卡切换标签页时只是切换「展示哪个 collector」。

时长语义：
- 消息类条目 duration = 下一条消息 timestamp − 本条 timestamp；同秒注入
  （hook 连发）为 0 — UI 侧对消息类显示绝对时间而非时长。
- TOOL / ASSISTANT 优先用实时信号给的精确起止。
- ``TraceRecord.duration_ms`` 不对已完成记录回退 ``time.time()``（P023）。
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
    estimate_tokens_text,
    infer_message_kind,
    is_real_user_message,
    message_label,
    message_source,
    truncate,
)


class TraceCollector(QObject):
    """常驻 Qt 对象：订阅一个对话窗口的 backend 信号，维护该会话的轨迹快照。"""

    # 会话切换 / 不可恢复变化 → UI 全量重置
    recordsReset = pyqtSignal()
    # 尾部追加（start, count）— 相对已落盘投影 _records
    recordsAppended = pyqtSignal(int, int)
    # 区段回填变化（start, count）— timing 回填 / 状态收尾
    recordsUpdated = pyqtSignal(int, int)
    # in-flight 尾部临时记录变化（仅在 tail 内容实际变化时发射）
    tailChanged = pyqtSignal()

    def __init__(self, parent: QObject = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._tail: List[TraceRecord] = []
        # tool_call_id → {"start", "end", "success", "name", "args", "result"}
        self._timing: Dict[str, Dict[str, Any]] = {}
        # assistant 流列表（按完成序）：{"start": float, "end": float}
        self._streams: List[Dict[str, float]] = []
        # 首次投影时已存在的 assistant 消息数（历史消息数），实时流序号 = k - base
        self._stream_base = 0
        self._active_session_id: str = ""
        self._bound_backend: Optional[Any] = None
        self._bound_main_widget: Optional[Any] = None
        # (消息序号, 文本长度) → token 数
        self._token_cache: Dict[tuple, int] = {}

    # ──────────────────── 公共 API ────────────────────

    def attach(self, backend: Any, main_widget: Any) -> None:
        """挂到指定 backend / 主窗口（幂等，backend 变化时重挂）。"""
        if backend is None:
            return
        if self._bound_backend is backend:
            self._bound_main_widget = main_widget
            return
        self.detach()
        self._bound_backend = backend
        self._bound_main_widget = main_widget
        try:
            backend.tool_call_started.connect(self._on_tool_call_started)
            backend.tool_result_received.connect(self._on_tool_result_received)
            backend.stream_started.connect(self._on_stream_started)
            backend.stream_finished.connect(self._on_stream_finished)
            # 主程序在 hook 注入 / 工具结果写入 messages 后触发的节拍信号
            if hasattr(backend, "_hook_messages_updated"):
                backend._hook_messages_updated.connect(self._on_messages_updated)
        except Exception as e:
            logger.warning(f"[agent_trace] attach signals failed: {e}")
        self._sync(emit_reset=True)

    def detach(self) -> None:
        """解除所有信号连接。"""
        if self._bound_backend is None:
            return
        for sig, slot in (
            ("tool_call_started", self._on_tool_call_started),
            ("tool_result_received", self._on_tool_result_received),
            ("stream_started", self._on_stream_started),
            ("stream_finished", self._on_stream_finished),
            ("_hook_messages_updated", self._on_messages_updated),
        ):
            try:
                getattr(self._bound_backend, sig).disconnect(slot)
            except TypeError, RuntimeError:
                pass  # 未连接 / 已析构
        self._bound_backend = None
        self._bound_main_widget = None

    @property
    def bound_main_widget(self) -> Optional[Any]:
        return self._bound_main_widget

    @property
    def records(self) -> List[TraceRecord]:
        """已落盘消息的稳定投影。"""
        return list(self._records)

    @property
    def tail(self) -> List[TraceRecord]:
        """in-flight 尾部临时记录（流式生成中 / 工具执行中）。"""
        return list(self._tail)

    @property
    def visible_records(self) -> List[TraceRecord]:
        """UI 展示列表 = 稳定投影 + in-flight 尾巴。"""
        return self._records + self._tail

    @property
    def has_pending(self) -> bool:
        return any(r.is_pending for r in self._tail)

    # ──────────────────── 会话同步 ────────────────────

    def refresh(self) -> None:
        """外部驱动的一次全量同步（切回标签页时调用）。"""
        self._sync()

    def reset(self) -> None:
        """清空采集缓存并重新投影 — 对应 UI 顶栏「清除」按钮。

        只清 **运行时缓存**（tool timing / stream timing / in-flight 尾巴），
        **不动 session.messages**：消息历史是事实源，清了就没了。
        清完立即从 messages 重新投影并 emit recordsReset。
        """
        self._records = []
        self._tail = []
        self._timing.clear()
        self._streams.clear()
        self._stream_base = 0
        self._sync(emit_reset=True)

    def _current_session(self) -> Optional[Any]:
        be = self._bound_backend
        if be is None:
            return None
        try:
            return be.get_current_session()
        except Exception:
            return None

    def _on_messages_updated(self, *_args: Any) -> None:
        try:
            self._sync()
        except Exception as e:
            logger.warning(f"[agent_trace] _sync failed: {e}\n{traceback.format_exc()}")

    def _sync(self, emit_reset: bool = False) -> None:
        """从 session.messages 全量重建投影，与旧列表 diff 后发增量信号。"""
        session = self._current_session()
        if session is None:
            if self._records or emit_reset:
                self._clear_all()
                self.recordsReset.emit()
            return

        sid = getattr(session, "session_id", "")
        if sid and sid != self._active_session_id:
            self._reset_runtime_state(sid)
            emit_reset = True

        messages = getattr(session, "messages", None) or []
        sys_prompt = ""
        try:
            sys_prompt = (getattr(session, "system_prompt", "") or "").strip()
        except Exception:
            pass
        new_records = self._project_messages(messages, system_prompt=sys_prompt)

        if emit_reset or not self._records:
            self._records = new_records
            # 重置点重算流基线：基线之前的历史 assistant 消息永远不会有实时流
            # （修「历史 assistant 消息抢占新流 timing → LLM 时长全 0」的序号错位）
            self._stream_base = sum(1 for r in new_records if r.kind == EntryKind.ASSISTANT)
            self.recordsReset.emit()
            self._set_tail(self._tail)  # clear/reset 可能清了 tail → 让 UI 收敛
            return

        old = self._records
        n_common = min(len(old), len(new_records))
        first_diff = n_common
        for i in range(n_common):
            if self._signature(old[i]) != self._signature(new_records[i]):
                first_diff = i
                break

        if first_diff == n_common and len(new_records) >= len(old):
            # 纯尾部追加
            self._records = new_records
            if len(new_records) > len(old):
                self.recordsAppended.emit(len(old), len(new_records) - len(old))
        elif len(new_records) == len(old):
            # 等长回填（timing 补齐 / 状态收尾）
            self._records = new_records
            if first_diff < n_common:
                self.recordsUpdated.emit(first_diff, n_common - first_diff)
        else:
            # 头部内容变化 / 缩短（截断、压缩等）→ 整体重置
            self._records = new_records
            self.recordsReset.emit()

        # hook 注入落盘后，清掉「hook 执行中」的尾巴（经 _set_tail 判重）
        if self._tail:
            self._set_tail([r for r in self._tail if r.kind != EntryKind.CONTEXT])

    def _clear_all(self) -> None:
        self._records = []
        self._set_tail([])
        self._timing.clear()
        self._streams.clear()
        self._stream_base = 0
        self._active_session_id = ""
        self._token_cache.clear()

    def _reset_runtime_state(self, sid: str) -> None:
        self._clear_all()
        self._active_session_id = sid

    @staticmethod
    def _signature(r: TraceRecord) -> tuple:
        """轻量签名 — diff 判断内容是否变化。"""
        return (r.kind, r.label[:32], round(r.start_ts, 3), round(r.end_ts, 3), r.is_error, r.is_pending, r.raw[:64])

    @staticmethod
    def _tail_signature(tail: List[TraceRecord]) -> tuple:
        return tuple((r.kind, r.label[:32], round(r.start_ts, 3), r.is_pending, r.raw[:48]) for r in tail)

    def _set_tail(self, new_tail: List[TraceRecord]) -> None:
        """tail 稳定化赋值：内容没变不发信号（修「历史记录一直在刷新」）。"""
        new_sig = self._tail_signature(new_tail)
        if new_sig == self._tail_signature(self._tail):
            return
        self._tail = list(new_tail)
        self.tailChanged.emit()

    # ──────────────────── messages → records 投影 ────────────────────

    def _project_messages(self, messages: List[Dict[str, Any]], system_prompt: str = "") -> List[TraceRecord]:
        records: List[TraceRecord] = []
        turn = 0
        assistant_seq = 0
        # 解析全部时间戳（秒级）作为条目起始时刻
        ts_list = [self._parse_timestamp(m.get("timestamp")) or 0.0 for m in messages]

        for i, msg in enumerate(messages):
            kind = infer_message_kind(msg)
            label = message_label(msg)
            raw_text = content_to_text(msg.get("content"))
            start = ts_list[i]
            # 消息写入本身是瞬时事件：默认 end=0（时长 0）。
            # ⚠️ 不能用「下一条时刻 − 本条时刻」当存续间隔 —— 用户隔 1 小时
            # 再问下一条，上一条消息就会显示 1 小时时长（闲置时间被算成时长）。
            # 真实时长只来自 TOOL/ASSISTANT 的实时 timing 回填（毫秒级）。
            end = 0.0

            meta: Dict[str, Any] = {}
            is_error = False

            if kind == EntryKind.TOOL:
                tool_call_id = msg.get("tool_call_id") or ""
                meta["tool_call_id"] = tool_call_id
                t = self._timing.get(tool_call_id)
                if t:
                    # 实时信号给的精确起止优先
                    if t.get("start"):
                        start = t["start"]
                    if t.get("end"):
                        end = t["end"]
                    is_error = not t.get("success", True)
                    meta["name"] = t.get("name") or label
                    if t.get("args"):
                        meta["arguments"] = t["args"]
                    if t.get("result"):
                        meta["result"] = t["result"]
                        raw_text = f"{t['args'] or ''}\n\n── result ──\n{t['result']}".strip()
                else:
                    meta["name"] = msg.get("name") or msg.get("tool_name") or "tool"
                    args_obj = msg.get("arguments")
                    if args_obj:
                        try:
                            meta["arguments"] = (
                                json.dumps(args_obj, ensure_ascii=False) if not isinstance(args_obj, str) else args_obj
                            )
                        except Exception:
                            meta["arguments"] = str(args_obj)
                preview = self._tool_preview(meta.get("arguments") or "", raw_text)

            elif kind == EntryKind.ASSISTANT:
                if msg.get("tool_calls"):
                    meta["has_tool_calls"] = True
                # 第 k 条 assistant 消息 ↔ 第 (k - _stream_base) 个实时流；
                # k < base 的是历史消息（collector 出生前落盘），无流可配
                s = None
                if assistant_seq >= self._stream_base:
                    si = assistant_seq - self._stream_base
                    if si < len(self._streams):
                        s = self._streams[si]
                assistant_seq += 1
                if s:
                    if s.get("start"):
                        start = s["start"]
                    if s.get("end"):
                        end = s["end"]
                preview = truncate(raw_text, 140) if raw_text.strip() else ""
                if not preview and msg.get("tool_calls"):
                    names = []
                    try:
                        for tc in msg["tool_calls"]:
                            n = (tc.get("function") or {}).get("name") or ""
                            if n:
                                names.append(n)
                    except Exception:
                        pass
                    preview = "→ 调用工具: " + ", ".join(names) if names else "→ 工具调用"
                if not preview:
                    preview = "（空）"
            else:
                preview = truncate(raw_text, 140)

            if is_real_user_message(msg):
                turn += 1
                meta["turn_start"] = True

            # token 占用（列表 Tokens 列）：逐条缓存，key = (消息序号, 文本长度)
            meta["tokens"] = self._tokens_for(i, raw_text)

            records.append(
                TraceRecord(
                    kind=kind,
                    label=label,
                    preview=preview,
                    raw=raw_text,
                    source=message_source(i, label),
                    start_ts=start,
                    end_ts=end,
                    is_pending=False,
                    is_error=is_error,
                    turn_no=turn,
                    meta=meta,
                )
            )

        # 合成 SYSTEM 条目：DriFox 的系统提示词不在 session.messages 里
        # （引擎构建请求时由 context_builder 动态拼装），只存在
        # session.system_prompt 属性上 → 不合成的话轨迹里永远看不到。
        # ⚠️ 条件只看「有没有消息」而不是「有没有 system_prompt」：SYSTEM 行是
        # 详情面板 System Prompt / Tools Schema 的唯一入口，会话即使没设系统
        # 提示词也应能查看当前挂载的工具 schema。
        if records and not any(r.kind == EntryKind.SYSTEM for r in records):
            head_ts = records[0].start_ts if records else 0.0
            records.insert(
                0,
                TraceRecord(
                    kind=EntryKind.SYSTEM,
                    label="System Prompt",
                    preview=truncate(system_prompt, 140) if system_prompt else "（无系统提示词 · 可查看 Tools Schema）",
                    raw=system_prompt,
                    source="session.system_prompt",
                    start_ts=head_ts,
                    end_ts=head_ts,
                    turn_no=0,
                ),
            )
        return records

    def _tokens_for(self, msg_index: int, text: str) -> int:
        """带缓存的 token 估算。

        tiktoken 编码不算便宜，而 ``_sync`` 会随 hook 高频触发 → 必须缓存。
        key 用 ``(消息序号, 文本长度)``：消息内容改写时长度几乎必然变化。
        """
        if not text:
            return 0
        key = (msg_index, len(text))
        hit = self._token_cache.get(key)
        if hit is not None:
            return hit
        value = estimate_tokens_text(text)
        if len(self._token_cache) > 4000:  # 防止长会话无限增长
            self._token_cache.clear()
        self._token_cache[key] = value
        return value

    @staticmethod
    def _tool_preview(args_text: str, raw_text: str) -> str:
        """TOOL 行预览：优先参数 JSON 摘要，其次结果首行。"""
        if args_text:
            return truncate(args_text.replace("\n", " "), 140)
        return truncate(raw_text, 140)

    @staticmethod
    def _parse_timestamp(s: Any) -> Optional[float]:
        """把 'YYYY-MM-DD HH:MM:SS[.f]' 解析为 epoch，失败返回 None。"""
        if not s or not isinstance(s, str):
            return None
        import datetime as _dt

        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return _dt.datetime.strptime(s, fmt).timestamp()
            except ValueError:
                continue
        return None

    # ──────────────────── 实时信号 → timing 表 / tail ────────────────────

    def _on_tool_call_started(self, tool_call_id: str, tool_name: str, arguments: Dict[str, Any]) -> None:
        if not tool_call_id or tool_call_id in self._timing:
            return
        try:
            args_text = json.dumps(arguments, ensure_ascii=False) if arguments else ""
        except Exception:
            args_text = str(arguments or "")
        self._timing[tool_call_id] = {
            "start": time.time(),
            "end": 0.0,
            "success": True,
            "name": tool_name or "tool",
            "args": args_text,
            "result": "",
        }
        self._append_tail(
            TraceRecord(
                kind=EntryKind.TOOL,
                label=tool_name or "tool",
                preview=args_text or "（调用中…）",
                raw=args_text,
                source=f"tool · {tool_name}",
                start_ts=time.time(),
                is_pending=True,
                meta={"tool_call_id": tool_call_id},
            )
        )

    def _on_tool_result_received(self, tool_call_id: str, name: str, result: Any, success: bool) -> None:
        t = self._timing.get(tool_call_id)
        if t is not None:
            t["end"] = time.time()
            t["success"] = bool(success)
            t["result"] = content_to_text(result)
        # 落盘消息通常在 result 信号之后写入，这里先清尾巴，_sync 由
        # _hook_messages_updated 驱动；若已落盘则立即同步回填。
        self._set_tail([r for r in self._tail if r.meta.get("tool_call_id") != tool_call_id])
        if t is None:
            # 结果先于 started 到达的兜底：直接记 timing 并同步
            self._timing[tool_call_id or f"anon-{time.time()}"] = {
                "start": 0.0,
                "end": time.time(),
                "success": bool(success),
                "name": name or "tool",
                "args": "",
                "result": content_to_text(result),
            }
        self._sync()

    def _on_stream_started(self) -> None:
        self._streams.append({"start": time.time(), "end": 0.0})
        self._append_tail(
            TraceRecord(
                kind=EntryKind.ASSISTANT,
                label="Assistant",
                preview="正在生成…",
                raw="",
                source="stream",
                start_ts=time.time(),
                is_pending=True,
            )
        )

    def _on_stream_finished(self, _payload: Dict[str, Any]) -> None:
        now = time.time()
        for s in reversed(self._streams):
            if not s.get("end"):
                s["end"] = now
                break
        self._set_tail([r for r in self._tail if not (r.kind == EntryKind.ASSISTANT and r.is_pending)])
        self._sync()

    def _append_tail(self, rec: TraceRecord) -> None:
        self._set_tail(self._tail + [rec])


class TraceCollectorHub(QObject):
    """per-window 常驻采集器管理（轨迹卡单实例持有）。

    每个对话标签页的 backend 各挂一个 :class:`TraceCollector`，**后台持续
    收集**（不展示的标签页也在记 timing）→ 切换标签页时精确耗时数据不丢。
    """

    def __init__(self, parent: QObject = None) -> None:
        super().__init__(parent)
        self._collectors: Dict[str, TraceCollector] = {}

    def collector_for(self, main_widget: Any) -> Optional[TraceCollector]:
        """取（或创建）绑定该窗口的 collector；窗口/ backend 未就绪返回 None。"""
        if main_widget is None:
            return None
        wid = getattr(main_widget, "_window_id", "") or ""
        if not wid:
            return None
        backend = getattr(main_widget, "backend", None)
        if backend is None:
            logger.debug("[agent_trace] hub: backend 未就绪，延迟建 collector")
            return None
        c = self._collectors.get(wid)
        if c is None:
            c = TraceCollector(self)
            c.attach(backend, main_widget)
            self._collectors[wid] = c
            logger.debug(f"[agent_trace] hub: 为窗口 {wid[:8]} 创建 collector")
        elif c._bound_backend is not backend:
            # 同窗口 backend 被替换 → 重挂（attach 幂等保留 timing）
            c.attach(backend, main_widget)
        return c

    def get(self, window_id: str) -> Optional[TraceCollector]:
        return self._collectors.get(window_id)

    def cleanup_closed(self, active_window_ids: Optional[set] = None) -> None:
        """清理已关闭窗口的 collector（切标签页时顺手调用）。"""
        for wid in list(self._collectors.keys()):
            c = self._collectors[wid]
            mw = c.bound_main_widget
            gone = mw is None
            if not gone:
                try:
                    mw.windowHandle()  # C++ 已析构时抛 RuntimeError
                except RuntimeError:
                    gone = True
                except Exception:
                    pass
            if not gone and active_window_ids is not None:
                gone = wid not in active_window_ids
            if gone:
                try:
                    c.detach()
                except RuntimeError:
                    pass
                c.deleteLater()
                del self._collectors[wid]
                logger.debug(f"[agent_trace] hub: 清理窗口 {wid[:8]} 的 collector")

    def dispose(self) -> None:
        """销毁全部 collector（卡片析构时调用）。"""
        self.cleanup_closed(active_window_ids=set())
