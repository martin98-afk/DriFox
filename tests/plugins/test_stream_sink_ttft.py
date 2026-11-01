# -*- coding: utf-8 -*-
"""openai_chat sink 的 TTFT 打点回归。

背景（2026-11-01 真实 bug）：sink 只在 ``content_delta`` / ``reasoning_delta``
记 TTFT。纯工具调用（模型不吐正文/思考，直接 function_call）整条没有 ttft_ms，
落盘后页脚拿 ``elapsed_ms``（含 prefill/排队，长上下文实测 8~35s）当生成时间，
29 tok / 35s 被算成 0.8 tok/s，把会话均值从 40+ 拉到个位数。

工具调用首个 chunk（``tool_call_begin``）即首 token 到达，必须记 TTFT。
"""

import importlib.util
import threading
import time
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parent.parent.parent / "plugins" / "system-stream-sinks"
_MODULE_PATH = _PLUGIN / "stream_sinks" / "openai_chat.py"

def _load():
    spec = importlib.util.spec_from_file_location("openai_chat_sink_ttft_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class _Tracker:
    def record_usage(self, usage):
        pass

class _Ctx:
    """sink 消费所需的最小状态口（属性名对齐 SinkContext 契约）。"""

    DEFERRED_PREVIEW_TOOLS = set()

    def __init__(self, req_elapsed_s: float = 1.5):
        self.stream_lock = threading.Lock()
        self.response = None
        self.current_response = None
        self.stream = True
        self.response_content_blocks = []
        self.current_tool_calls = {}
        self.tool_calls_buffer = {}
        self.tool_calls_index_to_id = {}
        self.response_chunks = []
        self.chunks_total_len = 0
        self.reasoning_content = ""
        self.reasoning_chunks = []
        self.last_usage = None
        self.cache_tracker = _Tracker()
        self.waiting_tool_params = {}
        self.previewed_tool_call_ids = set()
        self.last_progress_len = {}
        self.last_progress_ts = {}
        self.last_est_len = {}
        self.last_line_est = {}
        self.is_cancelled = False
        self.streaming_rss_base = 0.0
        self.mem_total_chunks_logged = 0
        self.last_ttft_ms = 0.0
        self.llm_req_t0 = time.monotonic() - req_elapsed_s
        self.mem_diag_enabled = False
        self.accumulated_tokens = 0
        self.max_param_retry_count = 3
        self.tool_start_callback = None
        self.token_update_callback = None
        self.emitted = []

    def emit(self, signal_name, *args):
        self.emitted.append((signal_name, args))

    def get_reasoning_content(self):
        return self.reasoning_content

    def extract_thought_signature(self, tc):
        return ""

@pytest.fixture(scope="module")
def mod():
    return _load()

def _tool_only_events(sink_mod):
    """纯工具调用流：无 content_delta / reasoning_delta。"""
    return [
        sink_mod.StreamEvent(type="tool_call_begin", tool_call_id="c1", name="grep", index=0),
        sink_mod.StreamEvent(type="tool_args_delta", tool_call_id="c1", text='{"pattern": "x"}', index=0),
        sink_mod.StreamEvent(type="finish", finish_reason="tool_calls"),
    ]

def test_tool_only_stream_records_ttft(mod):
    """纯工具调用也必须落 TTFT（首 chunk = 首 token）。"""
    ctx = _Ctx(req_elapsed_s=1.5)
    mod.OpenAIChatStreamSink().consume(_tool_only_events(mod), ctx)
    assert ctx.last_ttft_ms > 1000.0, f"纯工具调用漏记 TTFT: {ctx.last_ttft_ms}"
    assert ctx.last_ttft_ms < 5000.0

def test_content_still_records_ttft(mod):
    """原有正文路径不受影响：首个 content_delta 记 TTFT。"""
    ctx = _Ctx(req_elapsed_s=0.8)
    events = [
        mod.StreamEvent(type="content_delta", text="你好"),
        mod.StreamEvent(type="finish", finish_reason="stop"),
    ]
    mod.OpenAIChatStreamSink().consume(events, ctx)
    assert 500.0 < ctx.last_ttft_ms < 3000.0
