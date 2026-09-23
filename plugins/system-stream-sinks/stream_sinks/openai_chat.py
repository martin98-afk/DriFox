# -*- coding: utf-8 -*-
"""OpenAI chat/completions 流式事件接收器 — 系统插件实现（id="openai_chat"）。

职责边界（两层契约的第二层）：
- 接收 transport 归一后的 StreamEvent 流
- 驱动 worker 状态机（_process_response 原地搬迁，行为逐点等价）
- 不负责：协议形状识别（属 ProtocolTransport）、请求组装、重试/取消

历史修复一栏（搬迁不重写，全部保留）：
- Qwen/DashScope 并行 tool_call：index→id 映射、孤立 buffer 跳过避免死锁
- Gemini thought_signature：随 tool_call_done 透传
- 截断/过滤/空响应检测（length/content_filter/异常空流 → StreamInterruptedError）
- 超长 arguments JSON 解析：>1000 字符跳过逐块解析，避免 O(n²) 异常开销
- 流式结束回收期：超 60 秒或重试 10 次放弃解析，保留原始 arguments
- 流式 RSS 增量 > 200MB 自适应 GC + 流结束 final gc.collect
- batch 阈值：reasoning 20 字/80ms、content 30 字/80ms（性能优化）
- 工具调用出现时强制冲刷内容批处理（避免内容排队到工具执行后才出现）
- 优先 raw_id 匹配 buffer 失败后回落 index map（避免孤立 chunk 错合并）
- 新增 buffer 必须含 name（孤立 delta chunk 跳过）
"""

from __future__ import annotations

import gc
import json
import os
import time
from typing import Any, Dict, Iterable, Optional, Set, Tuple

# 顶层常量与 lazy import 兼容（worker 内通过 from app.core.workers.chat_worker 导入）
try:
    import psutil as _psutil

    _HAS_PSUTIL = True
except ImportError:
    _psutil = None
    _HAS_PSUTIL = False

from loguru import logger

from app.core.conversation.message_content import append_text_block


# StreamInterruptedError 定义在 app.core.workers.chat_worker。
# 为避免循环 import（sink 也可能在 worker 模块初始化阶段被加载），在这里做 lazy import：
def _get_stream_interrupted_error():
    from app.core.workers.chat_worker import StreamInterruptedError

    return StreamInterruptedError


class OpenAIChatStreamSink:
    """OpenAI chat/completions 流式事件接收器（默认实现）。

    consume(events, ctx) → (tool_calls_found, tool_args_pending)
    events 为 transport.to_events 产出的 StreamEvent 迭代器。
    ctx 为 worker 适配层，暴露：
      - 状态：current_tool_calls / tool_calls_buffer / tool_calls_index_to_id /
        response_content_blocks / response_chunks / chunks_total_len /
        reasoning_content / reasoning_chunks / last_usage / cache_tracker /
        waiting_tool_params / previewed_tool_call_ids / last_progress_len /
        last_progress_ts / last_est_len / last_line_est /
        is_cancelled / streaming_rss_base / mem_total_chunks_logged /
        current_response / stream_lock / last_ttft_ms /
        llm_req_t0 / max_param_retry_count / mem_diag_enabled /
        token_update_callback / accumulated_tokens / stream
      - 回调：tool_start_callback
      - 工具集合：DEFERRED_PREVIEW_TOOLS
      - emit(name, *args)：发射原 Qt 信号同名事件
    """

    id = "openai_chat"

    def consume(self, events: Iterable[Any], ctx: Any) -> Tuple[bool, bool]:
        """消费 StreamEvent 序列，行为等价于原 chat_worker._process_response。

        返回 (tool_calls_found, tool_args_pending)：给 worker 决定后续工具执行流。
        """
        # 🛡️ 保存响应引用，供 cancel() 安全中断流式等待。
        # 锁内写入：与 _abort_current_stream 的锁内读取配对，避免 cancel 抢在
        # 赋值前进锁读到 None 而跳过 shutdown（取消即时性降级为等下一个 chunk）。
        with ctx.stream_lock:
            ctx.current_response = ctx.response  # 形参由 worker 适配层注入
        ctx.response_content_blocks = []
        ctx.current_tool_calls = {}  # 改成字典，key 是 tool_call_id
        ctx.tool_calls_buffer = {}
        # Qwen/DashScope 流式 tool_calls：chunk 2+ 会清空 tc.id，用 index→id 映射回真实 id
        ctx.tool_calls_index_to_id = {}
        tool_calls_found = False
        tool_args_pending = True
        reasoning_started_this_call = False  # 本轮 API 调用是否已发射 thinking_started
        _reasoning_batch = ""  # 批量积累 reasoning，减少信号频率
        _reasoning_batch_time = time.time()  # 上次发射时间
        _content_batch = ""  # 批量积累 content，减少信号频率
        _content_batch_time = time.time()  # 上次发射 content 的时间
        chunk_count = 0  # 事件计数器（原 chunk 计数器），用于定期 mem diag
        ctx.mem_total_chunks_logged = 0  # 累计流式 chunk 计数
        # 🛡️ 流式结束校验：跟踪最后一个 chunk 的 finish_reason，识别服务端截断/过滤
        # 修复前从不检查 finish_reason：max_tokens 截断（length）/内容过滤（content_filter）
        # 被静默当「正常完成」→ 回复到一半无报错停止（工具调用迭代最易触达截断）
        last_finish_reason = None
        saw_any_chunk = False  # 是否收到过任意事件（区分空迭代 vs 仅 usage/finish）
        # 流式开始时记录 RSS 基线（用于自适应 GC）
        ctx.streaming_rss_base = 0.0
        if _HAS_PSUTIL:
            try:
                ctx.streaming_rss_base = _psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            except Exception:
                pass

        def _flush_content_batch():
            """强制冲刷 content 批次（检测到 tool_call 时清空，避免文本滞留）。"""
            nonlocal _content_batch, _content_batch_time
            if _content_batch:
                ctx.emit("content_received", _content_batch)
                _content_batch = ""
                _content_batch_time = time.time()

        def _emit_reasoning_batch(force: bool = False):
            nonlocal _reasoning_batch, _reasoning_batch_time
            if not _reasoning_batch:
                return
            ctx.emit("reasoning_content_received", _reasoning_batch)
            _reasoning_batch = ""
            _reasoning_batch_time = time.time()

        def _check_mem_diag():
            """每 100 事件触发一次 mem diag 日志 + 自适应 GC（不计数）。"""
            # 每处理 5 个 chunk 就让渡一次 CPU，确保主线程能及时处理排队的 Qt 信号
            # 避免 content_received 等信号堆积到工具执行完毕后一次性处理
            # 原注释保留（搬迁字面）
            # 每处理 100 个 chunk 记录一次流式内存快照
            if chunk_count % 100 == 0 and ctx.mem_diag_enabled:
                # [T28 L4] 计数器替代 sum（每 100 chunk 一次，长响应下省 O(chunks)）
                chunks_total = ctx.chunks_total_len
                ctx.mem_total_chunks_logged += 1
                rss_str = ""
                if _HAS_PSUTIL:
                    try:
                        rss = _psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
                        rss_str = f"rss={rss:.1f}MB "
                    except Exception:
                        pass
                logger.debug(
                    f"[MEM] streaming chunk#{chunk_count} "
                    f"{rss_str}"
                    f"_response_chunks#{len(ctx.response_chunks)} "
                    f"~{chunks_total // 1024}KB tool_calls@{len(ctx.current_tool_calls)}"
                )

                # 自适应 GC：每 100 chunk 收集一次，RSS 增量 > 200MB 时堆压缩
                # 🔧 修复：仅在 MEM_DIAG 启用时才执行 gc.collect()，避免无条件 stop-the-world GC 阻塞 UI
                if chunk_count % 100 == 0 and ctx.mem_diag_enabled:
                    freed = gc.collect()
                    if freed > 10:
                        logger.debug(f"[MEM] 流式 gc.collect() 释放了 {freed} 个对象")
                    # 在此作用域内获取 RSS，不依赖外部块
                    _gc_rss = 0.0
                    if _HAS_PSUTIL:
                        try:
                            _gc_rss = _psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
                        except Exception:
                            pass
                    if _gc_rss > 0 and ctx.streaming_rss_base > 0 and (_gc_rss - ctx.streaming_rss_base) > 200:
                        _delta = _gc_rss - ctx.streaming_rss_base
                        try:
                            # ⚠️ 这里原本还有 msvcrt._heapmin() 与 kernel32.HeapCompact(heap, 0)。
                            # 项目内 T10 实测结论：Python 3.14 下 HeapCompact **100% 抛 access
                            # violation**。该 AV 虽会落到下面的 except 被吞掉，但 Windows 进程堆
                            # 已被触碰过，进程之后处于不可信状态——后续任意分配/释放都可能随机
                            # 崩溃，而崩溃点会落在完全无关的代码上（例如 openai/_streaming.py），
                            # 极难归因。更何况这段代码跑在 worker 线程，与主线程/Qt 的并发堆
                            # 操作叠加，风险更高。堆压缩的收益远不抵风险，只保留纯 Python 侧 GC。
                            gc.collect()  # 触发 pymalloc arena 合并
                            logger.info(f"[MEM] 流式 RSS 增量 {_delta:.0f}MB>200MB，已触发自适应 GC")
                        except Exception as e:
                            logger.debug(f"[MEM] 自适应 GC 失败: {e}")
            # processEvents() 从 worker 线程调用仅处理 worker 线程自身事件，
            # 不会处理主线程事件队列中的跨线程 Qt 信号，因此对内容渲染无帮助。
            # 核心修复见上方「检测到 tool_calls 时强制冲刷 _content_batch」。
            # if chunk_count % 10 == 0:
            #     QCoreApplication.processEvents()

        for ev in events:
            saw_any_chunk = True
            etype = ev.type

            if ctx.is_cancelled:
                # 🛡️ 取消前刷新待处理的 content/reasoning 批次，避免丢失最后一批内容
                if _reasoning_batch:
                    ctx.emit("reasoning_content_received", _reasoning_batch)
                    _reasoning_batch = ""
                _flush_content_batch()
                return (False, False)  # 返回元组而不是单个布尔值

            # ---------- usage 事件（可能与 finish 同 chunk，独立 finish_reason） ----------
            if etype == "usage":
                usage = ev.usage or {}
                # transport 已归一为 dict；worker 旧逻辑同时维护 _last_usage 与 tracker
                if usage:
                    ctx.last_usage = {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    }
                    # 同步更新缓存追踪器（CacheHitRateTracker.record_usage 兼容 dict）
                    try:
                        ctx.cache_tracker.record_usage(usage)
                    except Exception:
                        pass
                chunk_count += 1
                _check_mem_diag()
                continue

            # ---------- finish 事件（流末尾，携带 finish_reason） ----------
            if etype == "finish":
                _fr = ev.finish_reason or None
                if _fr:
                    last_finish_reason = _fr
                chunk_count += 1
                _check_mem_diag()
                continue

            # ---------- content_delta：正文增量 ----------
            if etype == "content_delta":
                content = ev.text or ""
                # 📊 TTFT：首个有实际内容的事件（思考/正文/工具调用）距请求发出的延迟。
                if ctx.last_ttft_ms <= 0.0 and content:
                    try:
                        ctx.last_ttft_ms = round((time.monotonic() - ctx.llm_req_t0) * 1000, 1)
                    except Exception:
                        pass
                if content:
                    # 性能优化：使用 list append + join 代替字符串拼接
                    ctx.response_chunks.append(content)
                    ctx.chunks_total_len += len(content)
                    ctx.response_content_blocks = append_text_block(ctx.response_content_blocks, content)
                    # [PERF] 批量发送：积累到 30 字符或 80ms 才 emit，降低信号频率
                    # 原 15 字符/50ms 过于激进，每 50ms 触发一次完整的
                    # content_received → _on_content_received → append_chunk → _schedule_render → setHtml 链
                    # 增大阈值后大幅减少 WebEngine 渲染次数，用户感知的流式流畅度无显著影响
                    _content_batch += content
                    now = time.time()
                    if len(_content_batch) >= 30 or (now - _content_batch_time) > 0.08:
                        ctx.emit("content_received", _content_batch)
                        _content_batch = ""
                        _content_batch_time = now
                chunk_count += 1
                _check_mem_diag()
                continue

            # ---------- reasoning_delta：思考增量 ----------
            if etype == "reasoning_delta":
                reasoning_delta = ev.text or ""
                # 📊 TTFT：reasoning 也算实际内容
                if ctx.last_ttft_ms <= 0.0 and reasoning_delta:
                    try:
                        ctx.last_ttft_ms = round((time.monotonic() - ctx.llm_req_t0) * 1000, 1)
                    except Exception:
                        pass
                if reasoning_delta:
                    if not reasoning_started_this_call:
                        reasoning_started_this_call = True
                        ctx.emit("thinking_started")  # 原 signal 无参数
                    # 性能优化：使用 list append 代替字符串拼接
                    ctx.reasoning_chunks.append(reasoning_delta)
                    # [PERF] 批量发送：积累到 20 字符或 80ms 才 emit，降低信号频率
                    # 原 10 字符/50ms 过高，导致流式时每 50ms 触发一次 Qt 跨线程信号+WebEngine 重渲染
                    # 增大到 20 字符/80ms 后信号频率降低 37.5%（50ms→80ms），
                    # 字符阈值 10→20 在中文场景下约多等 5-10 字，用户无明显感知
                    _reasoning_batch += reasoning_delta
                    now = time.time()
                    if len(_reasoning_batch) >= 20 or (now - _reasoning_batch_time) > 0.08:
                        ctx.emit("reasoning_content_received", _reasoning_batch)
                        _reasoning_batch = ""
                        _reasoning_batch_time = now
                chunk_count += 1
                _check_mem_diag()
                continue

            # ---------- tool_call_begin：首 chunk 带 id+name（含可能空 arguments） ----------
            if etype == "tool_call_begin":
                tool_calls_found = True

                # 🔧 检测到 tool_calls 时，强制冲刷已积累的内容批处理缓冲
                # 避免：文本因不满 30 字符/80ms 阈值而滞留到流式结束才 emit，
                # 然后流结束立即进入工具执行，导致内容 signal 排队在工具 signal 之后，
                # 用户感知为"文本要等工具执行完才出现"
                _flush_content_batch()

                # 取事件字段（保持与原 chunk 形状一致：raw_id / tc_index / name / sig）
                raw_id = ev.tool_call_id or ""
                tc_index = ev.index if ev.index != -1 else None
                # thought_signature 直接从事件读取（原 self._extract_thought_signature(tc)）
                sig = ev.thought_signature or ""
                # transport 已在非空 name 时才发 tool_call_begin，所以这里 name 必为非空
                tool_name = ev.name or ""

                tc_id = None

                # 1. 优先用 raw_id 匹配现有 buffer
                if raw_id and raw_id in ctx.tool_calls_buffer:
                    tc_id = raw_id
                # 2. 否则用 index 映射回真实 id（处理 qwen 等 id 缺失场景）
                elif tc_index is not None and tc_index in ctx.tool_calls_index_to_id:
                    tc_id = ctx.tool_calls_index_to_id[tc_index]

                # ⚠️ 关键修复（2026-06-25 qwen 工具永远卡在"接收参数中"）：
                # 修复前代码 `elif self._tool_calls_buffer: tc_id = next(reversed(...))`
                # 会把第二个 tool_call 的内容错合并到第一个 buffer，导致：
                # 1) 多 tool_call 并行时 name 互相覆盖
                # 2) Qwen 末尾 `id=""` 的孤立 chunk 被合并进已有 buffer
                # 新逻辑：找不到匹配 buffer 时，必须含 name 才创建新条目，避免孤立 buffer
                # 累积导致 tool_args_pending 永远 True、主循环死锁。
                if not tc_id:
                    # 必须含 name 才允许创建新 buffer（孤立 delta chunk 跳过）
                    if not tool_name:
                        chunk_count += 1
                        _check_mem_diag()
                        continue
                    # 用真实 id 作为 key（缺 id 时退化用 index）
                    tc_id = raw_id if raw_id else (f"index_{tc_index}" if tc_index is not None else None)
                    if not tc_id:
                        chunk_count += 1
                        _check_mem_diag()
                        continue

                if tc_id not in ctx.tool_calls_buffer:
                    ctx.tool_calls_buffer[tc_id] = {
                        "id": tc_id,
                        "type": "function",  # 原 getattr(tc, "type", "function")，chat/completions 固定
                        "function": {"name": "", "arguments": ""},
                    }
                    ctx.response_content_blocks.append(
                        {
                            "type": "tool_call_marker",
                            "tool_call_id": tc_id,
                        }
                    )
                    # 记录 index → id 映射（供后续 chunk 查找）
                    if tc_index is not None:
                        ctx.tool_calls_index_to_id[tc_index] = tc_id

                buffer = ctx.tool_calls_buffer[tc_id]
                # 🔧 Gemini thought_signature：必须随 tool call 透传，否则多轮工具调用会 400。
                # 签名可能出现在任意 delta（通常与 id+name 同片，也可能在 arguments 之后的独立片），
                # 这里每次 delta 都尝试提取并落到 buffer / _current_tool_calls。
                if sig:
                    buffer["thought_signature"] = sig
                    if tc_id in ctx.current_tool_calls:
                        ctx.current_tool_calls[tc_id]["thought_signature"] = sig

                if tool_name:
                    buffer["function"]["name"] = tool_name

                    # 收到 tool name 时立即添加到 _current_tool_calls（如果是新工具）
                    if tc_id not in ctx.current_tool_calls:
                        ctx.current_tool_calls[tc_id] = {
                            "id": tc_id,
                            "type": buffer.get("type", "function"),
                            "function": {
                                "name": tool_name,
                                "arguments": "",
                            },
                            "thought_signature": buffer.get("thought_signature"),
                        }

                    if (
                        tool_name
                        and tool_name not in ctx.DEFERRED_PREVIEW_TOOLS
                        and tc_id not in ctx.previewed_tool_call_ids
                    ):
                        ctx.previewed_tool_call_ids.add(tc_id)
                        # preview 阶段：arguments 可能还没接收完，显示 "加载中..." 而不是空 {}
                        preview_args = {"_status": "loading"}
                        if ctx.tool_start_callback:
                            ctx.tool_start_callback(tc_id, tool_name, preview_args, "preview")
                        else:
                            ctx.emit(
                                "tool_call_started",
                                tc_id,
                                tool_name,
                                preview_args,
                                "preview",
                            )

                # tool_call_begin 通常不携带 arguments（transport 仅在非空时发 tool_args_delta），
                # 但可能 args_delta 紧随其后或跨事件出现，这里不重复解析。
                # 参数累加/解析在 tool_args_delta 分支处理（保持与原代码块行为对齐）。

                chunk_count += 1
                _check_mem_diag()
                continue

            # ---------- tool_args_delta：后续 chunk 仅 arguments（可能含 name / id 清空） ----------
            if etype == "tool_args_delta":
                tool_calls_found = True

                # 🔧 同 tool_call_begin：出现 tool args 即强制冲刷内容批处理
                _flush_content_batch()

                raw_id = ev.tool_call_id or ""
                tc_index = ev.index if ev.index != -1 else None
                args_delta = ev.text or ""

                tc_id = None

                # 1. 优先用 raw_id 匹配现有 buffer
                if raw_id and raw_id in ctx.tool_calls_buffer:
                    tc_id = raw_id
                # 2. 否则用 index 映射回真实 id（处理 qwen 等 id 缺失场景）
                elif tc_index is not None and tc_index in ctx.tool_calls_index_to_id:
                    tc_id = ctx.tool_calls_index_to_id[tc_index]

                # ⚠️ 关键修复（同 tool_call_begin 分支）：找不到匹配 buffer 时，必须含 name 才创建
                # 注意：tool_args_delta 的 ev.name 通常为空（transport 不在 args 事件上带 name），
                # 因此 args_delta 默认跳过孤立 delta chunk（若上游顺序异常才允许基于 name fallback）
                if not tc_id:
                    if not args_delta:
                        chunk_count += 1
                        _check_mem_diag()
                        continue
                    # 缺 index_map 的晚期孤立 chunk 视为噪声跳过
                    chunk_count += 1
                    _check_mem_diag()
                    continue

                buffer = ctx.tool_calls_buffer[tc_id]

                if args_delta:
                    # ⚠️ Qwen 末尾 chunk 的 arguments=null，跳过避免 TypeError
                    buffer["function"]["arguments"] += args_delta

                # 【优化】不在此处逐 chunk 执行 json.loads()。
                # 对于 write/edit 等超长 content 参数，arguments 可能分 50-200 个 chunks 到达。
                # 每次全量 json.loads() 都会失败并产生异常开销。
                # 改为流结束后在 _process_response 末尾一次性解析。
                if buffer["function"]["name"] and buffer["function"]["arguments"]:
                    # 仅在累积字符串达到一定长度时才尝试预解析（用于更新预览状态）
                    # 对于超长场景（>1000 字符），跳过所有逐块解析，等流结束再做
                    args_len = len(buffer["function"]["arguments"])
                    if args_len <= 1000:
                        try:
                            parsed_args = json.loads(buffer["function"]["arguments"])
                            tool_args_pending = False
                            # 更新 _current_tool_calls 中对应 id 的 arguments
                            if tc_id in ctx.current_tool_calls:
                                ctx.current_tool_calls[tc_id]["function"]["arguments"] = buffer["function"]["arguments"]
                            # 标记已完成解析（用于决定是否发送 tool_call_started）
                            ctx.current_tool_calls[tc_id]["_args_parsed"] = True
                            ctx.tool_calls_buffer.pop(tc_id, None)
                            # 流式中间状态：推送实际参数到 UI 更新预览
                            # 使用 buffer 中的 name 而非局部 tool_name（后续 chunk 可能不含 name 字段）
                            _buf_name = buffer["function"].get("name", "")
                            ctx.emit(
                                "tool_args_updated",
                                tc_id,
                                _buf_name or "工具",
                                parsed_args,
                            )
                        except json.JSONDecodeError:
                            # 短参数的 JSON 解析失败，记录到等待队列
                            # 同时也发射长度进度，避免 UI 一直卡在"正在准备参数..."
                            from app.core.tools.tool_arg_lines import (
                                LINE_ESTIMATE_STEP,
                                build_progress_payload,
                                extract_partial_path,
                                should_emit_progress,
                            )

                            prev = ctx.last_progress_len.get(tc_id, 0)
                            _now_ms = time.monotonic() * 1000.0
                            if should_emit_progress(prev, args_len, ctx.last_progress_ts.get(tc_id, 0.0), _now_ms):
                                ctx.last_progress_len[tc_id] = args_len
                                ctx.last_progress_ts[tc_id] = _now_ms
                                _buf_name = buffer["function"].get("name", "")
                                # 行数按步长重算，未到步长沿用上次（超长参数下避免 O(n²) 扫描）
                                _est_len = ctx.last_est_len.get(tc_id, 0)
                                _reuse = bool(_est_len) and (args_len - _est_len) < LINE_ESTIMATE_STEP
                                # 编辑类工具顺带估算增删行数（运行框显示 +N/-M）+ 未闭合路径提前提取
                                progress_args, _est = build_progress_payload(
                                    _buf_name,
                                    buffer["function"]["arguments"],
                                    args_len,
                                    extract_partial_path(buffer["function"]["arguments"]),
                                    ctx.last_line_est.get(tc_id, (0, 0)),
                                    _reuse,
                                )
                                if not _reuse:
                                    ctx.last_est_len[tc_id] = args_len
                                ctx.last_line_est[tc_id] = _est
                                ctx.emit(
                                    "tool_args_updated",
                                    tc_id,
                                    _buf_name or "工具",
                                    progress_args,
                                )
                            if tc_id not in ctx.waiting_tool_params:
                                ctx.waiting_tool_params[tc_id] = {
                                    "buffer": buffer,
                                    "attempt_count": 0,
                                    "first_failure_time": time.time(),
                                }
                            ctx.waiting_tool_params[tc_id]["attempt_count"] += 1
                    else:
                        # 参数已超过 1000 字符，跳过逐块 JSON 解析以节省开销
                        # 但仍推送长度进度 + 累积尾部预览，让 UI 显示接收进度
                        from app.core.tools.tool_arg_lines import (
                            LINE_ESTIMATE_STEP,
                            build_progress_payload,
                            extract_partial_path,
                            should_emit_progress,
                        )

                        prev = ctx.last_progress_len.get(tc_id, 0)
                        _now_ms = time.monotonic() * 1000.0
                        if should_emit_progress(prev, args_len, ctx.last_progress_ts.get(tc_id, 0.0), _now_ms):
                            ctx.last_progress_len[tc_id] = args_len
                            ctx.last_progress_ts[tc_id] = _now_ms
                            _buf_name = buffer["function"].get("name", "")
                            # 行数按步长重算，未到步长沿用上次（超长参数下避免 O(n²) 扫描）
                            _est_len = ctx.last_est_len.get(tc_id, 0)
                            _reuse = bool(_est_len) and (args_len - _est_len) < LINE_ESTIMATE_STEP
                            # 编辑类工具顺带估算增删行数（运行框显示 +N/-M）+ 未闭合路径提前提取
                            progress_args, _est = build_progress_payload(
                                _buf_name,
                                buffer["function"]["arguments"],
                                args_len,
                                extract_partial_path(buffer["function"]["arguments"]),
                                ctx.last_line_est.get(tc_id, (0, 0)),
                                _reuse,
                            )
                            if not _reuse:
                                ctx.last_est_len[tc_id] = args_len
                            ctx.last_line_est[tc_id] = _est
                            ctx.emit(
                                "tool_args_updated",
                                tc_id,
                                _buf_name or "工具",
                                progress_args,
                            )
                        # 放入等待队列，等流结束后一次性解析
                        if tc_id not in ctx.waiting_tool_params:
                            ctx.waiting_tool_params[tc_id] = {
                                "buffer": buffer,
                                "attempt_count": 0,
                                "first_failure_time": None,  # None 表示流中不计算超时
                            }
                        ctx.waiting_tool_params[tc_id]["attempt_count"] += 1

                chunk_count += 1
                _check_mem_diag()
                continue

            # ---------- tool_call_done / 未知事件：原代码无对应路径，吞掉 ----------
            chunk_count += 1
            _check_mem_diag()
            continue

        # =====================================================================
        # 流式结束后处理（一次性解析等待中的 tool args / token usage / 截断检测）
        # =====================================================================

        # 非流式响应：usage 在 response 对象本身（而非事件流）
        if not ctx.stream:
            # 通过 ctx 获取 response 上 usage
            response = getattr(ctx, "response", None) or ctx.current_response
            usage = getattr(response, "usage", None) if response is not None else None
            if usage:
                ctx.last_usage = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                    "total_tokens": getattr(usage, "total_tokens", 0),
                }
                # 同步更新缓存追踪器
                try:
                    ctx.cache_tracker.record_usage(usage)
                except Exception:
                    pass
                total = getattr(usage, "total_tokens", 0) or 0
                ctx.accumulated_tokens += total
                # 实时通知外部（如 AutoLoop）更新 token 计数
                if ctx.token_update_callback and total > 0:
                    ctx.token_update_callback(total)
        # 冲刷剩余的 reasoning batch 和 content batch
        if _reasoning_batch:
            ctx.emit("reasoning_content_received", _reasoning_batch)
        _flush_content_batch()

        # 性能优化：移除从 worker 线程调用的 processEvents()
        # 跨线程信号传递由 Qt 的 QueuedConnection 自动处理，无需手动 processEvents
        # QCoreApplication.processEvents()

        # 处理等待完整参数的 tool_calls（超长 arguments 场景）
        # 在所有 chunk 接收完成后，再次尝试解析仍处于等待状态的 tool_calls
        # 性能优化：使用 update 代替创建临时集合
        all_pending_ids = set(ctx.tool_calls_buffer.keys())
        all_pending_ids.update(ctx.waiting_tool_params.keys())

        for tc_id in list(all_pending_ids):
            buffer = ctx.tool_calls_buffer.get(tc_id)
            waiting_info = ctx.waiting_tool_params.get(tc_id)

            # 如果 buffer 存在，优先使用 buffer
            if not buffer and waiting_info:
                buffer = waiting_info["buffer"]

            if buffer and buffer["function"]["name"] and buffer["function"]["arguments"]:
                args_str = buffer["function"]["arguments"]

                # 无论 tc_id 是否已存在，都尝试解析 JSON
                # fix: 已存在的 tc_id 也必须尝试解析，否则 tool_args_pending 无法设为 False
                if tc_id in ctx.current_tool_calls:
                    # 先更新 arguments
                    ctx.current_tool_calls[tc_id]["function"]["arguments"] = args_str

                try:
                    # 尝试 JSON 解析（参数完整时应当成功）
                    parsed_args = json.loads(args_str)
                    tool_args_pending = False
                    if tc_id not in ctx.current_tool_calls:
                        ctx.current_tool_calls[tc_id] = {
                            "id": buffer["id"],
                            "type": buffer.get("type", "function"),
                            "function": {
                                "name": buffer["function"]["name"],
                                "arguments": args_str,
                            },
                            "thought_signature": buffer.get("thought_signature"),
                            "_args_parsed": True,
                        }
                    else:
                        ctx.current_tool_calls[tc_id]["_args_parsed"] = True
                    # 从等待队列中移除
                    ctx.waiting_tool_params.pop(tc_id, None)
                    ctx.tool_calls_buffer.pop(tc_id, None)
                except json.JSONDecodeError as e:
                    # JSON 仍然解析失败，记录详细错误信息
                    if tc_id in ctx.tool_calls_buffer:
                        ctx.tool_calls_buffer.pop(tc_id, None)

                    # 检查是否超过最大重试次数
                    attempt_count = waiting_info.get("attempt_count", 0) if waiting_info else 0
                    first_time = waiting_info.get("first_failure_time", 0) if waiting_info else 0
                    wait_duration = time.time() - first_time if first_time else 0

                    # 超过 60 秒或超过 10 次尝试，放弃解析
                    # fix: 放弃解析时也设置 tool_args_pending = False，避免无限循环
                    if wait_duration > 60 or attempt_count >= ctx.max_param_retry_count:
                        logger.warning(
                            f"[ToolCall] ⚠️ JSON 解析超时/超限，保留原始 arguments: "
                            f"tool={buffer['function']['name']}, "
                            f"args_len={len(args_str)}, "
                            f"attempt_count={attempt_count}, "
                            f"wait_duration={wait_duration:.1f}s, "
                            f"error={str(e)}, "
                            f"preview='{args_str[:100]}...'"
                        )
                        # 保留原始 arguments 字符串，让后续处理决定如何处理
                        if tc_id not in ctx.current_tool_calls:
                            ctx.current_tool_calls[tc_id] = {
                                "id": buffer["id"],
                                "type": buffer.get("type", "function"),
                                "function": {
                                    "name": buffer["function"]["name"],
                                    "arguments": args_str,  # 保留原始字符串
                                },
                                "thought_signature": buffer.get("thought_signature"),
                            }
                        # fix: 放弃解析时标记参数不再 pending，允许继续执行
                        tool_args_pending = False
                        ctx.waiting_tool_params.pop(tc_id, None)
                    else:
                        # 还在等待中，保持在等待队列
                        if tc_id not in ctx.waiting_tool_params:
                            ctx.waiting_tool_params[tc_id] = {
                                "buffer": buffer,
                                "attempt_count": attempt_count + 1,
                                "first_failure_time": first_time,
                            }

        # fix: 所有待处理项都处理完毕后，如果没有任何剩余等待项，标记 args_pending = False
        if tool_calls_found and not ctx.tool_calls_buffer and not ctx.waiting_tool_params:
            tool_args_pending = False
            # 确保所有已识别的 tool call 都有原始 arguments（防止参数被跳过导致为空字符串）
            for tc in ctx.current_tool_calls.values():
                if not tc["function"]["arguments"] and tc.get("id") in all_pending_ids:
                    tc["function"]["arguments"] = "{}"

        # 流式结束后清理 index→id 临时映射（仅流处理期间需要）
        ctx.tool_calls_index_to_id = {}

        # 🛡️ 清除当前响应引用（已完成，不需要被 cancel 关闭）
        if ctx.current_response is not None:
            ctx.current_response = None

        # ========== 流式结束校验：识别服务端截断/过滤/异常空响应 ==========
        # 修复前：不检查 finish_reason，服务端截断被静默当「正常完成」，
        # 用户看到回复到一半无报错停止（工具调用迭代最易触达 max_tokens 截断）。
        # 注意：取消路径（_is_cancelled 分支 return (False, False)）不会走到这里；
        # 空迭代（无任何 chunk）保持原有行为（兼容测试 FakeEmptyResp），不误报。
        if not ctx.is_cancelled:
            if last_finish_reason == "length":
                # max_tokens 截断：回复不完整，明确提示（保留已接收 partial）
                raise _get_stream_interrupted_error()(
                    "[输出截断] 模型回复被 max_tokens 上限截断（finish_reason=length），"
                    "回复内容不完整。请调大「最大Token」设置，或让模型分步输出。"
                )
            if last_finish_reason == "content_filter":
                raise _get_stream_interrupted_error()(
                    "[内容过滤] 模型回复被内容安全过滤器拦截（finish_reason=content_filter），"
                    "已接收内容保留。请调整提问或回复内容后重试。"
                )
            if saw_any_chunk and last_finish_reason in (None, "stop") and not tool_calls_found:
                # 收到了事件但没有任何输出内容（无 content、无 reasoning、无 tool_calls）：
                # 可能是服务端异常提前结束（如过载/代理断开但 HTTP 层正常结束）
                has_text = bool(ctx.response_chunks or ctx.response_content_blocks)
                # _get_reasoning_content 等价：ctx 上暴露 reasoning_content / reasoning_chunks
                has_reasoning = bool(
                    "".join(ctx.reasoning_chunks)
                    if getattr(ctx, "reasoning_chunks", None)
                    else getattr(ctx, "reasoning_content", "") or ""
                )
                if not has_text and not has_reasoning:
                    raise _get_stream_interrupted_error()(
                        "[空响应] 模型返回了空响应（未生成任何内容），可能是服务端过载或网络异常。请稍后重试。"
                    )

        # 🔧 修复：流式结束后立即回收临时对象（ChatCompletionChunk/Choice/Delta 链）
        # httpx+OpenAI 客户端在处理 900+ chunk 时创建大量临时 Python 对象，
        # 这些对象在此处已无引用，但 pymalloc arena 碎片仍然占用 RSS。
        # 主动 gc.collect() + Windows HeapCompact 可降低峰值 RSS。
        # 注意：仅在 MEM_DIAG 启用时才执行 gc.collect()，避免无条件 stop-the-world GC 阻塞 UI
        if ctx.mem_diag_enabled:
            freed_count = gc.collect()
            if freed_count > 100:
                logger.debug(f"[MEM] 流式结束 gc.collect() 释放了 {freed_count} 个对象")

        return (tool_calls_found, tool_args_pending)


def register(registry):
    """插件注册入口（stream_sinks 组件，runtime_component_loader 扫描调用）"""
    registry.register(OpenAIChatStreamSink())
