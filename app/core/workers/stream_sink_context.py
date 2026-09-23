# -*- coding: utf-8 -*-
"""流式接收器上下文 — worker 侧适配层（StreamEventSink.consume 的 ctx 参数）。

设计取舍：
- sink 插件需要读写 worker 的 20+ 个私有状态（原 _process_response 直接访问），
  且这些状态是「流式过程量」，逐字段搬迁成公开 API 会带来巨量样板与新的不一致风险。
- 故本适配层用 __getattr__/__setattr__ 代理到 worker 的对应私有属性
  （`ctx.current_tool_calls` ↔ `worker._current_tool_calls`），
  语义与搬迁前逐点一致；emit 则包装 _emit_with_callback（信号对象由适配层查表补齐）。

不可变量：ctx 不修改 worker 的任何属性名，只做命名映射。
"""

from __future__ import annotations

from typing import Any

# worker 侧「私有属性 → ctx 公开名」映射（sink 搬迁时按此访问）
_ATTR_MAP = {
    "response_content_blocks": "_response_content_blocks",
    "current_tool_calls": "_current_tool_calls",
    "tool_calls_buffer": "_tool_calls_buffer",
    "tool_calls_index_to_id": "_tool_calls_index_to_id",
    "response_chunks": "_response_chunks",
    "reasoning_content_attr": "_reasoning_content",
    "reasoning_chunks": "_reasoning_chunks",
    "last_usage": "_last_usage",
    "cache_tracker": "_cache_tracker",
    "waiting_tool_params": "_waiting_tool_params",
    "previewed_tool_call_ids": "_previewed_tool_call_ids",
    "last_progress_len": "_last_progress_len",
    "last_progress_ts": "_last_progress_ts",
    "last_est_len": "_last_est_len",
    "last_line_est": "_last_line_est",
    "is_cancelled": "_is_cancelled",
    "streaming_rss_base": "_streaming_rss_base",
    "mem_total_chunks_logged": "_mem_total_chunks_logged",
    "current_response": "_current_response",
    "stream_lock": "_stream_lock",
    "chunks_total_len": "_chunks_total_len",
    "last_ttft_ms": "_last_ttft_ms",
    "llm_req_t0": "_llm_req_t0",
    "mem_diag_enabled": "_mem_diag_enabled",
    "tool_execution_cancelled": "_tool_execution_cancelled",
    "accumulated_tokens": "_accumulated_tokens",
}

# 信号名 → worker 上的信号属性名（emit 时查表拿信号对象，兼容 UI 层依赖）
_SIGNAL_ATTRS = {
    "content_received": "content_received",
    "reasoning_content_received": "reasoning_content_received",
    "tool_call_started": "tool_call_started",
    "tool_args_updated": "tool_args_updated",
    "tool_call_completed": "tool_call_completed",
    "thinking_started": "thinking_started",
    "usage_updated": "usage_updated",
    "tool_result_received": "tool_result_received",
}


class StreamSinkContext:
    """worker 状态适配层：sink 经本对象读写 worker 流式过程量。"""

    # 运行时字段：由 worker 在每次 consume 前注入（非 worker 属性，故不查表）
    _RUNTIME_FIELDS = ("response", "stream", "token_update_callback", "reasoning_content")

    def __init__(self, worker: Any) -> None:
        object.__setattr__(self, "_worker", worker)
        object.__setattr__(self, "_runtime", {})
        # 透传的常量/回调（只读）。统一走 worker.__dict__ 直取：
        # 未初始化 PyQt 对象（测试 __new__ 构造）上 getattr/hasattr 会触发
        # RuntimeError: super-class __init__() was never called
        wdict = getattr(worker, "__dict__", {})
        object.__setattr__(self, "DEFERRED_PREVIEW_TOOLS", wdict.get("_DEFERRED_PREVIEW_TOOLS", set()))
        object.__setattr__(self, "tool_start_callback", wdict.get("tool_start_callback"))
        object.__setattr__(self, "max_param_retry_count", wdict.get("_max_param_retry_count", 5))

    def bind(self, *, response: Any = None, stream: bool = True, token_update_callback: Any = None) -> None:
        """worker 在每次 consume 前注入本次调用的运行时字段。

        - response：本次响应流对象（sink 写入 current_response 供 cancel 中断用）
        - stream：本次是否流式（sink 据此跳过 streaming-only 逻辑）
        - token_update_callback：token 用量回调（可选）
        """
        runtime = object.__getattribute__(self, "_runtime")
        runtime["response"] = response
        runtime["stream"] = stream
        runtime["token_update_callback"] = token_update_callback

    # ---------- 属性代理 ----------

    def __getattr__(self, name: str) -> Any:
        runtime = object.__getattribute__(self, "_runtime") if name in self._RUNTIME_FIELDS else None
        if runtime is not None and name in runtime:
            return runtime[name]
        worker = object.__getattribute__(self, "_worker")
        wdict = getattr(worker, "__dict__", {})
        mapped = _ATTR_MAP.get(name)
        if mapped is not None:
            if mapped in wdict:
                return wdict[mapped]
            return getattr(worker, mapped)
        if name == "reasoning_content":
            # 原 worker 属性 `_reasoning_content`（_ATTR_MAP 里名为 reasoning_content_attr，
            # 此处为 sink 的直读名保留专用分支）
            return wdict.get("_reasoning_content", "")
        # 未登记的名字走同名私有属性（兼容搬迁时的漏登记，读路径宽松）
        private = f"_{name}"
        if private in wdict:
            return wdict[private]
        if hasattr(worker, private):
            return getattr(worker, private)
        raise AttributeError(f"StreamSinkContext 无属性 {name!r}（worker 亦无 {private!r}）")

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") and name in ("_worker", "_runtime"):
            object.__setattr__(self, name, value)
            return
        if name in self._RUNTIME_FIELDS:
            runtime = object.__getattribute__(self, "_runtime")
            runtime[name] = value
            return
        mapped = _ATTR_MAP.get(name)
        worker = object.__getattribute__(self, "_worker")
        if mapped is not None:
            setattr(worker, mapped, value)
            return
        private = f"_{name}"
        # 已登记映射直接写；未登记的名字经 __dict__ 判定（hasattr 在未初始化 PyQt 对象上会抛）
        if private in getattr(worker, "__dict__", {}):
            setattr(worker, private, value)
            return
        object.__setattr__(self, name, value)

    # ---------- 信号发射 ----------

    def emit(self, signal_name: str, *args: Any) -> None:
        """替代 worker._emit_with_callback(name, signal, *args)：信号对象由本层补齐。"""
        worker = object.__getattribute__(self, "_worker")
        attr = _SIGNAL_ATTRS.get(signal_name, signal_name)
        # __dict__ 直取：PyQt 信号是类属性，未初始化实例上 getattr 会抛
        # RuntimeError: super-class __init__() was never called；类属性经 __dict__ 取不到
        # 时回退 getattr（已初始化实例的正常路径）
        signal = worker.__dict__.get(attr)
        if signal is None:
            try:
                signal = getattr(worker, attr, None)
            except RuntimeError:
                signal = None
        worker._emit_with_callback(signal_name, signal, *args)

    def get_reasoning_content(self) -> str:
        """等价 worker._get_reasoning_content()（截断检测用）"""
        worker = object.__getattribute__(self, "_worker")
        return worker._get_reasoning_content()

    def extract_thought_signature(self, tc: Any) -> str:
        """等价 worker._extract_thought_signature(tc)（Gemini 签名提取）。

        sink 搬迁后事件已带 thought_signature 字段，本方法保留供兼容路径使用。
        """
        worker = object.__getattribute__(self, "_worker")
        return worker._extract_thought_signature(tc) or ""
