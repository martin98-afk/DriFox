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
    GAP_CAP_S,
    MIN_SPAN_S,
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

        # assistant 已有正式落盘记录时，同步清掉「正在生成」尾巴 —— README 承诺的
        # 「落盘后自动被正式记录取代」。只靠 stream_finished 清会在 finished
        # 丢失的路径（手动停止 / 异常中断）留下永久走动时长的僵尸尾巴。
        n_assistant = sum(1 for r in new_records if r.kind == EntryKind.ASSISTANT)
        if self._tail and n_assistant > self._stream_base:
            self._set_tail([r for r in self._tail if not (r.kind == EntryKind.ASSISTANT and r.is_pending)])

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
            # 毫秒时间戳优先：``timestamp`` 只有秒级精度，同秒注入的多条消息
            # 无法区分先后（hook 连发时尤其明显）→ ts_ms 由主程序写入（见
            # chat_session / backend / chat_worker 三处写入点）。
            start = _epoch_from_ts_ms(msg.get("ts_ms")) or start
            # 消息写入本身是瞬时事件：默认 end=0（时长 0）。
            # ⚠️ 不能用「下一条时刻 − 本条时刻」当存续间隔 —— 用户隔 1 小时
            # 再问下一条，上一条消息就会显示 1 小时时长（闲置时间被算成时长）。
            # 真实时长只来自 TOOL/ASSISTANT 的实时 timing 回填（毫秒级）。
            end = 0.0

            meta: Dict[str, Any] = {}
            is_error = False

            if kind == EntryKind.ASSISTANT:
                # 真实 token 用量优先：worker 会把 API 响应的 usage 落成
                # msg["token_usage"] = {"input","output","total"}（chat_worker）。
                # ⚠️ 口径：input = prompt_tokens = 本次请求的**完整上下文**
                # （系统提示 + 全部历史 + 本轮提问），total 随会话增长且包含
                # 之前所有轮次 → 不能当「本条回复占用」显示（每条都像总量）。
                # Tokens 列用 output（completion_tokens = 本次回复实际输出），
                # 与无 usage 时的文本估算口径一致（都是本条内容）；
                # output 缺失 / 为 0（estimated 兜底）时回退文本估算。
                usage = msg.get("token_usage")
                if isinstance(usage, dict):
                    output = usage.get("output")
                    if isinstance(output, (int, float)) and output > 0:
                        meta["tokens"] = int(output)
                        meta["tokens_exact"] = True
                # 思维链（DeepSeek V4 / GLM-5 thinking mode）：worker 落盘在
                # msg["reasoning_content"]，详情面板靠它出 Thinking tab；
                # 分段耗时的 reasoning 阶段在 meta["phases"]（worker 落盘）。
                reasoning = msg.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning.strip():
                    meta["reasoning"] = reasoning

            if kind == EntryKind.TOOL:
                tool_call_id = msg.get("tool_call_id") or ""
                meta["tool_call_id"] = tool_call_id
                # 分阶段耗时（perm=权限含弹窗等待 / exec=执行 / other / total）
                phases = msg.get("trace_phases")
                if isinstance(phases, dict) and phases:
                    meta["phases"] = dict(phases)
                t = self._timing.get(tool_call_id)
                args_text = ""
                if t:
                    # 实时信号给的精确起止优先
                    if t.get("start"):
                        start = t["start"]
                    if t.get("end"):
                        end = t["end"]
                    is_error = not t.get("success", True)
                    meta["name"] = t.get("name") or label
                    args_text = t.get("args") or ""
                    if t.get("result"):
                        meta["result"] = t["result"]
                else:
                    meta["name"] = msg.get("name") or msg.get("tool_name") or "tool"
                if end <= 0 and isinstance(phases, dict) and phases:
                    # 没有实时 end（重新加载会话 / 历史消息）→ 用持久化的总耗时
                    # 反推起止：ts_ms = 结果返回时刻，起点 = 它 − total。
                    total = phases.get("total")
                    if isinstance(total, (int, float)) and total > 0 and start > 0:
                        end = start
                        start = start - total / 1000.0
                if not args_text:
                    # 实时参数是占位（流式未接收完，见 _is_placeholder_args）或
                    # 根本没有 timing → 用**落盘消息**的 arguments 兜底：
                    # tool 消息写入时参数必然已解析完整，这才是最可靠的来源。
                    args_obj = msg.get("arguments")
                    if args_obj:
                        try:
                            args_text = (
                                json.dumps(args_obj, ensure_ascii=False) if not isinstance(args_obj, str) else args_obj
                            )
                        except Exception:
                            args_text = str(args_obj)
                if args_text:
                    meta["arguments"] = args_text
                if meta.get("result"):
                    raw_text = f"{args_text}\n\n── result ──\n{meta['result']}".strip()
                preview = self._tool_preview(meta.get("arguments") or "", raw_text)

            elif kind == EntryKind.ASSISTANT:
                if msg.get("tool_calls"):
                    meta["has_tool_calls"] = True
                # ① 优先用 worker 落盘的真实耗时（elapsed_ms，毫秒）。
                #    这样**重新加载会话后耗时依然在**——实时信号测出来的
                #    值只存在于内存，重启即丢。
                #    ⚠️ ts_ms 是「响应完成」时刻（消息写入时打点），
                #    所以起点 = 完成时刻 − 耗时，终点 = 完成时刻。
                llm_ms = msg.get("elapsed_ms")
                if isinstance(llm_ms, (int, float)) and llm_ms > 0:
                    meta["elapsed_ms"] = float(llm_ms)
                    if start > 0:
                        end = start
                        start = start - llm_ms / 1000.0
                # ⚠️ 不再用实时流配对兜底：stream_started/stream_finished 是
                # **整个 worker 线程**级别的（executor.py 在 worker.start() 后
                # 只发一次），一轮含多次工具迭代时只有一对 start/end。按序号
                # 配对会把「整轮总时长」套到单条 assistant 上，实测出现过
                # 7m41s 的巨型条带覆盖半条时间线、列表里却找不到对应记录。
                # 没有 elapsed_ms 的历史消息宁可无耗时（— / span 语义），
                # 也不展示必然错误的值。_streams 仍保留给 tail 防重入用。
                assistant_seq += 1
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

            # token 占用（列表 Tokens 列）：优先用 worker 落盘的真实 usage，
            # 没有才按字符估算并逐条缓存（key = 消息序号 + 文本长度）
            if not meta.get("tokens"):
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
        # ⚠️ 必须在 SYSTEM 合成**之后**再算占用终点：SYSTEM 插在最前面，
        # 它会成为原第一条的「下一条」，直接影响第一条的 span。
        self._fill_span_ends(records)
        return records

    @staticmethod
    def _fill_span_ends(records: List[TraceRecord]) -> None:
        """给**没有真实耗时**的条目补占用终点 → 时间线连贯。

        规则（写 ``meta["span_end"]`` / ``meta["span_capped"]``）：
        - 有真实耗时（TOOL/ASSISTANT 由实时信号回填）→ 用 end_ts，不动；
        - 否则占用 = 到下一条起点的间隔，封顶 ``GAP_CAP_S``（用户思考很久不该
          把条带拉爆），封顶时打 ``span_capped`` 标记 → UI 显示 ``≥3.00 s``；
        - ⚠️ 间隔为 0（**同秒注入**，消息时间戳只有秒级精度）就是瞬时事件，
          span = 0。早期版本保底 80ms，结果时长列只剩「80ms / 3s」两个怪值。
        """
        for i, rec in enumerate(records):
            if rec.start_ts <= 0:
                continue
            if rec.end_ts > rec.start_ts:
                rec.meta["span_end"] = rec.end_ts
                rec.meta.pop("span_capped", None)
                continue
            nxt = records[i + 1] if i + 1 < len(records) else None
            gap = 0.0
            if nxt is not None and nxt.start_ts > rec.start_ts:
                gap = min(nxt.start_ts - rec.start_ts, GAP_CAP_S)
            rec.meta["span_end"] = rec.start_ts + max(gap, MIN_SPAN_S)
            if gap >= GAP_CAP_S:
                rec.meta["span_capped"] = True
            else:
                rec.meta.pop("span_capped", None)

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
    def _result_success(result: Any) -> bool:
        """从工具结果里判定成功与否。

        引擎回调给的 ``result`` 形态不固定（dict / 带 success 属性的对象 /
        纯文本），只能逐个探测；探测不到就当成功（宁可不标红，也不误报）。
        """
        if isinstance(result, dict):
            for key in ("success", "ok", "is_error"):
                if key in result:
                    v = result[key]
                    return (not bool(v)) if key == "is_error" else bool(v)
            if result.get("error"):
                return False
            return True
        for attr in ("success", "ok"):
            v = getattr(result, attr, None)
            if isinstance(v, bool):
                return v
        err = getattr(result, "error", None)
        return not bool(err)

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
        """工具开始 —— 同一个 tool_call_id 会来**两次**（见下方 ⚠️）。

        ⚠️ worker 在流式参数**还没接收完**时就先发一次预览，arguments 是占位
        字典 ``{"_status": "loading"}``（chat_worker.py:3452），真实参数在
        ``_execute_tools_*`` 里解析完整后再发一次。旧代码用
        ``if tool_call_id in self._timing: return`` 直接挡掉第二次 → 所有工具的
        入参永远显示成 ``{"_status": "loading"}``，且计时起点早于真实执行。
        """
        if not tool_call_id:
            return

        placeholder = _is_placeholder_args(arguments)
        args_text = _dump_arguments(arguments)

        existing = self._timing.get(tool_call_id)
        if existing is not None:
            # 第二次到达：参数已解析完整 → 覆盖占位并重起计时（真实执行起点）
            if not placeholder:
                existing["args"] = args_text
                existing["start"] = time.time()
                existing.pop("args_placeholder", None)
                self._update_tail_args(tool_call_id, args_text)
            return

        self._timing[tool_call_id] = {
            "start": time.time(),
            "end": 0.0,
            "success": True,
            "name": tool_name or "tool",
            "args": args_text,
            "result": "",
        }
        if placeholder:
            self._timing[tool_call_id]["args_placeholder"] = True
        self._append_tail(
            TraceRecord(
                kind=EntryKind.TOOL,
                label=tool_name or "tool",
                preview=_pending_preview(args_text, placeholder),
                raw=args_text,
                source=f"tool · {tool_name}",
                start_ts=time.time(),
                is_pending=True,
                meta={
                    "tool_call_id": tool_call_id,
                    **({"args_placeholder": True} if placeholder else {}),
                },
            )
        )

    def _update_tail_args(self, tool_call_id: str, args_text: str) -> None:
        """真实参数到达后，同步刷新 in-flight 尾巴上那条记录（就地改，不重建列表）。"""
        for rec in self._tail:
            if rec.meta.get("tool_call_id") != tool_call_id:
                continue
            rec.meta.pop("args_placeholder", None)
            rec.raw = args_text
            rec.preview = _pending_preview(args_text, False)
            break
        self._set_tail(self._tail)

    def _on_tool_result_received(self, tool_call_id: str, name: str, arguments: Any, result: Any) -> None:
        """工具结果回调 —— 签名与引擎侧一致 ``(id, name, arguments, result)``。

        ⚠️ 旧签名是 ``(id, name, result, success)``，与 backend 实际 emit 的参数
        对不上（第 3 位是 arguments、第 4 位是 result 对象），success 只能自己
        从 result 里探测（:meth:`_result_success`）。
        """
        success = self._result_success(result)
        t = self._timing.get(tool_call_id)
        if t is not None:
            t["end"] = time.time()
            t["success"] = success
            t["result"] = content_to_text(result)
            # 结果回调带的 arguments 是解析后的真实值，可用来补齐；占位字典跳过
            if arguments and not _is_placeholder_args(arguments):
                text = _dump_arguments(arguments)
                if text and not t.get("args"):
                    t["args"] = text
        # 落盘消息通常在 result 回调之后写入，这里先清尾巴，_sync 由
        # _hook_messages_updated 驱动；若已落盘则立即同步回填。
        self._set_tail([r for r in self._tail if r.meta.get("tool_call_id") != tool_call_id])
        if t is None:
            # 结果先于 started 到达的兜底：直接记 timing 并同步
            self._timing[tool_call_id or f"anon-{time.time()}"] = {
                "start": 0.0,
                "end": time.time(),
                "success": success,
                "name": name or "tool",
                "args": "",
                "result": content_to_text(result),
            }
        self._sync()

    def _on_stream_started(self) -> None:
        # 🛡️ 幂等：已存在未闭合流 / pending assistant 尾巴时不重复登记。
        # 背景：stream_started 历史上被双发（executor + engine 各一次），
        # 重入会产生 2 条「正在生成」尾巴（时长一模一样、永久走动）。
        if any(not s.get("end") for s in self._streams):
            return
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
        # 闭合**所有**未闭合流：一次 finished 对应一次流会话，重入/双发留下的
        # end=0 僵尸流会被投影配对命中 → 记录时长为负/无限走动。
        for s in self._streams:
            if not s.get("end"):
                s["end"] = now
        self._set_tail([r for r in self._tail if not (r.kind == EntryKind.ASSISTANT and r.is_pending)])
        self._sync()

    def _append_tail(self, rec: TraceRecord) -> None:
        self._set_tail(self._tail + [rec])


def _is_placeholder_args(arguments: Any) -> bool:
    """是否「参数还没接收完」的占位字典。

    worker 在流式阶段会先发两类占位（chat_worker.py）：
    - ``{"_status": "loading"}`` / ``{"_status": "loading", "_args_len": N}`` —— 接收中
    - ``{"_raw_args": "...", "_status": "parse_failed"}`` —— 解析失败，但带原始截断
    两者都**不是真实入参**，不能直接展示在 Request 面板里。
    """
    return isinstance(arguments, dict) and "_status" in arguments


def _dump_arguments(arguments: Any) -> str:
    """参数序列化成可展示文本；占位字典只保留有信息量的 ``_raw_args``。"""
    if arguments is None:
        return ""
    if _is_placeholder_args(arguments):
        raw = arguments.get("_raw_args")
        return str(raw) if raw else ""
    try:
        return json.dumps(arguments, ensure_ascii=False) if not isinstance(arguments, str) else arguments
    except Exception:
        return str(arguments)


def _pending_preview(args_text: str, placeholder: bool) -> str:
    """in-flight 行的预览文案。"""
    if args_text:
        return args_text
    return "（正在接收参数…）" if placeholder else "（调用中…）"


def _epoch_from_ts_ms(value: Any) -> Optional[float]:
    """毫秒时间戳 → epoch 秒；非法/缺失返回 None。

    ``timestamp`` 字段只有秒级精度，同秒注入的多条消息排不出先后，所以主程序
    额外写了毫秒级 ``ts_ms``。这里兼容秒级误写（值 < 1e11 当秒处理）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return float(value) / 1000.0 if value > 1e11 else float(value)


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
