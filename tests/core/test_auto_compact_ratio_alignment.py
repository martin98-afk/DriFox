# -*- coding: utf-8 -*-
"""auto-compact 触发占比与圆环显示口径对齐回归测试

背景 bug：hook 注入的 token_count 用 count_messages_tokens(session.messages)
（原始未截断全量、不含 tools/system），而圆环走 engine 快照
（工具结果截断投影 + API 精确值优先 + tools schema）。
重度工具会话下 hook 估算可达圆环 2~3 倍 → 圆环显示 50% 时 hook 已判 80%+，
自动压缩提前触发。

修复：tool_executor._get_context_usage_info / chat_worker PreAssistantMessage
注入统一改走 backend.get_context_usage_snapshot（与圆环同源）。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.tool_executor import ToolExecutor


def _make_executor(backend) -> ToolExecutor:
    """轻量构造 ToolExecutor（__new__ 绕过 __init__，仅初始化本路径依赖）"""
    ex = ToolExecutor.__new__(ToolExecutor)
    ex._backend = backend
    return ex


def _make_backend(session, snapshot_ret=None, snapshot_exc=None):
    backend = MagicMock()
    backend.get_current_session.return_value = session
    if snapshot_exc:
        backend.get_context_usage_snapshot.side_effect = snapshot_exc
    else:
        backend.get_context_usage_snapshot.return_value = snapshot_ret or {}
    backend._get_model_config.return_value = {"模型名称": "test-model"}
    return backend


def _make_session(messages, api_tokens=0, api_count=0, from_api=False):
    """构造带 API usage 快照属性的轻量 session"""
    s = SimpleNamespace(messages=list(messages))
    s.last_api_prompt_tokens = api_tokens
    s.last_api_message_count = api_count
    s.last_api_prompt_from_usage = from_api
    return s


class TestContextUsageInfoUsesEngineSnapshot:
    """_get_context_usage_info 必须与圆环同源（backend 快照）"""

    def test_snapshot_values_win(self):
        """快照返回 used/budget → 原样返回，不再用全量估算"""
        session = _make_session([{"role": "tool", "tool_call_id": "c1", "content": "x" * 40000}])
        backend = _make_backend(
            session,
            snapshot_ret={"used_tokens": 50000, "budget_tokens": 100000},
        )
        ex = _make_executor(backend)

        count, limit = ex._get_context_usage_info()

        assert (count, limit) == (50000, 100000)
        backend.get_context_usage_snapshot.assert_called_once()
        # 必须透传 session 的 API usage 三参数（与圆环 _refresh_context_usage_indicator 一致）
        _, kwargs = backend.get_context_usage_snapshot.call_args
        assert kwargs.get("api_prompt_tokens") == 0
        assert kwargs.get("from_api") is False

    def test_passes_api_usage_attrs(self):
        """session 的 last_api_prompt_tokens/from_api 必须透传给快照"""
        session = _make_session([{"role": "user", "content": "hi"}], api_tokens=12345, api_count=3, from_api=True)
        backend = _make_backend(session, snapshot_ret={"used_tokens": 12345, "budget_tokens": 200000})
        ex = _make_executor(backend)

        ex._get_context_usage_info()

        _, kwargs = backend.get_context_usage_snapshot.call_args
        assert kwargs.get("api_prompt_tokens") == 12345
        assert kwargs.get("api_message_count") == 3
        assert kwargs.get("from_api") is True

    def test_empty_snapshot_falls_back_to_estimate(self):
        """快照为空（无 engine）→ 回退旧本地估算，不能返回 (0,0) 丢功能"""
        # 40000 字符 ≈ 10k token 的工具结果：旧口径全额计入
        session = _make_session([{"role": "user", "content": "x" * 4000}])
        backend = _make_backend(session, snapshot_ret={})
        ex = _make_executor(backend)

        count, limit = ex._get_context_usage_info()

        assert count > 0
        assert limit > 0  # resolve_context_limit 兜底（models.dev 动态默认）

    def test_snapshot_exception_falls_back(self):
        """快照抛异常 → 回退旧估算（hook 不能因此失效）"""
        session = _make_session([{"role": "user", "content": "hello world"}])
        backend = _make_backend(session, snapshot_exc=RuntimeError("boom"))
        ex = _make_executor(backend)

        count, limit = ex._get_context_usage_info()

        assert count > 0
        assert limit > 0

    def test_no_session_returns_zero(self):
        """无 session 保持 (0, 0) 契约（hook 输出"无法获取"）"""
        backend = _make_backend(None)
        ex = _make_executor(backend)

        assert ex._get_context_usage_info() == (0, 0)


class TestHookRatioMatchesRing:
    """端到端口径验证：同一消息集，hook 侧 ratio ≈ 快照侧 ratio

    圆环口径：工具结果超 8192 字符截断后计数（engine 快照 S2 投影）。
    旧 hook 口径：原文计数 → 超长工具结果场景 ratio 显著偏大（本次 bug）。
    """

    def test_overlong_tool_result_not_inflated(self):
        """超长工具结果：hook 注入值必须等于快照 used（截断口径），而非原文估算"""
        from app.core.token_estimator import count_messages_tokens, per_message_tokens

        big = "line %d: data\n" * 3000  # ~21k 字符 ≈ 5k token
        messages = [
            {"role": "user", "content": "read it"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path":"a"}'}}
                ],
                "content": "",
            },
            {"role": "tool", "tool_call_id": "c1", "content": big},
        ]
        session = _make_session(messages)

        # 圆环（engine 快照）在无 API 值时的 used = est_total（含截断投影）
        # 这里不构建完整 engine，用同一截断函数构造快照返回值模拟圆环口径
        from app.core.context_builder import prune_tool_result

        approx = []
        for m in messages:
            if m.get("role") == "tool" and len(m.get("content", "")) > 8192:
                m2 = dict(m)
                m2["content"] = prune_tool_result(m["content"], tool_name="read")
                approx.append(m2)
            else:
                approx.append(m)
        ring_used = sum(per_message_tokens(m) for m in approx)

        backend = _make_backend(session, snapshot_ret={"used_tokens": ring_used, "budget_tokens": 128000})
        ex = _make_executor(backend)
        hook_count, _ = ex._get_context_usage_info()

        # 旧口径（原文）显著偏大 —— 证明 bug 场景存在
        raw_estimate = count_messages_tokens(messages)
        assert raw_estimate > hook_count * 1.5
        # 修复后 hook = 圆环口径
        assert hook_count == ring_used


class TestChatWorkerHookInjectionAlignment:
    """chat_worker PreAssistantMessage 注入同样走快照口径"""

    def test_injection_uses_snapshot(self):
        from app.core.workers.chat_worker import OpenAIChatWorker

        session = _make_session([{"role": "user", "content": "hi"}], api_tokens=777, api_count=1, from_api=True)
        backend = _make_backend(session, snapshot_ret={"used_tokens": 777, "budget_tokens": 1000})
        worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
        worker.llm_config = {"模型名称": "test-model"}

        count, limit = OpenAIChatWorker._hook_context_usage(worker, backend)

        assert (count, limit) == (777, 1000)
        _, kwargs = backend.get_context_usage_snapshot.call_args
        assert kwargs.get("api_prompt_tokens") == 777
        assert kwargs.get("from_api") is True

    def test_injection_fallback(self):
        from app.core.workers.chat_worker import OpenAIChatWorker

        session = _make_session([{"role": "user", "content": "x" * 500}])
        backend = _make_backend(session, snapshot_ret={})
        worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
        worker.llm_config = {"模型名称": "test-model"}

        count, limit = OpenAIChatWorker._hook_context_usage(worker, backend)

        assert count > 0 and limit > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
