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

from app.core.backend import ChatBackend


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


def test_deferred_create_sub_agent_idempotent(monkeypatch):
    """重复调用 _deferred_create_sub_agent_and_misc 不得覆盖 SubAgentManager。"""
    backend = _make_backend("win_dup_subagent_1")

    created = []
    subagent_cls = MagicMock()

    monkeypatch.setattr(
        "app.core.workers.subagent_worker.SubAgentManager",
        subagent_cls,
    )

    def fake_cls_factory(**kwargs):
        inst = MagicMock()
        created.append(inst)
        return inst

    subagent_cls.side_effect = fake_cls_factory

    backend._deferred_create_sub_agent_and_misc()
    first = backend._sub_agent_manager
    assert first is created[0]

    backend._deferred_create_sub_agent_and_misc()

    assert backend._sub_agent_manager is first, "重复创建覆盖了 _sub_agent_manager"
    assert len(created) == 1, f"SubAgentManager 被创建了 {len(created)} 次（应为 1 次）"
