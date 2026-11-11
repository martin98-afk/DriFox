# -*- coding: utf-8 -*-
"""ChatBackend 延迟创建幂等性回归测试。

背景（2026-09-15 团队成员无法对话）：团队 leader 窗口自动开场走
``ensure_deferred_components()`` 同步创建 ChatEngine 后，错峰的
``create_engines`` 队列任务再次执行 ``_deferred_create_engines()``。
无幂等守卫时重复创建覆盖 ``_chat_engine``，UI 回调 flush 进新实例，
而运行中的 worker 仍持有旧实例（回调字典为空）——表现为团队对话
后台正常推进、前端消息卡片全空白。
"""

from unittest.mock import MagicMock

from app.core.conversation.backend import ChatBackend


def _make_backend(window_id: str) -> ChatBackend:
    backend = ChatBackend(window_id=window_id)
    backend._tool_executor = MagicMock()
    backend._session_manager = MagicMock()
    backend._agent_manager = MagicMock()
    backend._get_model_config = lambda: {}
    return backend


def test_deferred_create_engines_idempotent(monkeypatch):
    """重复调用 _deferred_create_engines 不得覆盖已存在的 ChatEngine。"""
    backend = _make_backend("win_dup_engine_1")

    created = []

    def fake_create_engine_for_slot(slot, fallback_cls, **kwargs):
        engine = MagicMock()
        created.append(engine)
        return engine

    monkeypatch.setattr(
        "app.plugins.registries.engine_registry.create_engine_for_slot",
        fake_create_engine_for_slot,
    )
    # flush 依赖真实引擎方法，桩掉仅记录调用
    flush_calls = []
    monkeypatch.setattr(backend, "_flush_pending_engine_callbacks", lambda: flush_calls.append(1))

    backend._deferred_create_engines()
    first = backend._chat_engine
    assert first is created[0]

    # 模拟队列任务在 ensure_deferred_components 同步创建之后再次执行
    backend._deferred_create_engines()

    assert backend._chat_engine is first, "重复创建覆盖了 _chat_engine（幂等守卫失效）"
    assert len(created) == 1, f"ChatEngine 被创建了 {len(created)} 次（应为 1 次）"


def test_backend_holds_no_sub_agent_state(monkeypatch):
    """主程序不再持有子智能体任何状态（管理器归 system-subagent 插件）。"""
    backend = _make_backend("win_no_subagent_1")

    for name in (
        "sub_agent_manager",
        "_sub_agent_manager",
        "sub_agent_ready",
        "set_subagent_model_resolver",
        "set_sub_agent_history_getter",
        "ensure_sub_agent_manager",
    ):
        assert not hasattr(backend, name), f"backend 仍暴露子智能体字段/方法: {name}"

    # 600ms 批只剩 git 缓存预热，不再创建管理器
    backend._deferred_warm_git_cache()
    assert not hasattr(backend, "_sub_agent_manager")
