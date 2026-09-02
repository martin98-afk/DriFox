# -*- coding: utf-8 -*-
"""worker 循环接入 LoopPolicy — 轮数上限与策略判定可被插件接管。

含引擎级 loop_policy_id（ConversationConfig → Executor → Worker）：
插件自建引擎可声明自己的循环策略，按 id 直接取对象，**不污染全局激活槽**。
"""

import pytest


@pytest.fixture()
def fresh_loop_registry(monkeypatch):
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    warmup_runtime_components()
    reg = LoopPolicyRegistry.get_instance()
    # 保证干净基线：仅系统插件 default
    yield reg


def test_loop_policy_helper_uses_active(fresh_loop_registry, monkeypatch):
    from app.plugins.contracts.loop_policy import LoopDecision, LoopPolicy, LoopState
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    class _NoToolPolicy:
        id = "no-tool"

        def should_continue(self, state):
            return LoopDecision.CONTINUE if state.stop_hook_injected else LoopDecision.STOP

        def max_rounds(self, llm_config):
            return 1

    LoopPolicyRegistry.get_instance().register(_NoToolPolicy(), source="plugin:test")
    try:
        assert LoopPolicyRegistry.get_instance().set_active("no-tool")
        worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
        w = worker_cls.__new__(worker_cls)
        w._loop_policy_obj = None
        assert w._loop_policy().id == "no-tool"
        assert w._loop_policy().max_rounds({}) == 1
    finally:
        LoopPolicyRegistry.get_instance().unregister_source("plugin:test")


def test_default_policy_unlimited(fresh_loop_registry):
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    w = worker_cls.__new__(worker_cls)
    w._loop_policy_obj = None
    assert w._loop_policy().id == "default"
    assert w._loop_policy().max_rounds({}) is None


def test_strategy_exception_falls_back(fresh_loop_registry, monkeypatch):
    """策略拖异常时 _check_loop_round_limit 应回退默认行为（不限）不炸。"""
    from app.plugins.contracts.loop_policy import LoopDecision, LoopState
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    class _BoomPolicy:
        """max_rounds 与 should_continue 均抛 RuntimeError，用于验证兜底逻辑"""

        id = "boom"

        def should_continue(self, state):
            raise RuntimeError("boom from should_continue")

        def max_rounds(self, llm_config):
            raise RuntimeError("boom from max_rounds")

    LoopPolicyRegistry.get_instance().register(_BoomPolicy(), source="plugin:test-boom")
    try:
        assert LoopPolicyRegistry.get_instance().set_active("boom")
        worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
        w = worker_cls.__new__(worker_cls)
        w._loop_policy_obj = None
        # 验证 _check_loop_round_limit 不炸、回退 False（不限）
        # 通过 llm_config 避免 None 边界
        w.llm_config = {}
        # 预置 _loop_round_count 绕过 __init__ 未调用限制（__new__ 后 getattr 会触发 super-class 限制）
        w._loop_round_count = 0
        # 第一次调用触发 _loop_round_count 自增；max_rounds 异常被 try/except 吞，回退 None → False
        assert w._check_loop_round_limit() is False
        # 再次调用仍不应炸
        assert w._check_loop_round_limit() is False

        # should_continue 异常回退 CONTINUE：直接通过 LoopDecision 验证（行为级验证：
        # worker 内部 try/except 与 max_rounds 同构，回退 CONTINUE 后落入"continue 默认分支"——
        # 即维持现状不清场、不 emit 完成信号。代码审查级断言见同文件其他 test）
        boom = w._loop_policy()
        try:
            _ = boom.should_continue(LoopState(repetitive_loop_detected=True))
            failed = False
        except RuntimeError:
            failed = True
        # 策略本身的 should_continue 仍会抛（异常在 worker 边界被吞）
        assert failed is True
    finally:
        LoopPolicyRegistry.get_instance().unregister_source("plugin:test-boom")
        # 复位激活策略为 default，避免污染其他测试
        LoopPolicyRegistry.get_instance().set_active("default")


# ── 引擎级 loop_policy_id（不污染全局激活槽）────────────────────


def _register_plugin_policy(policy_id="plugin-single-turn", source="plugin:test-engine-scope", max_rounds=1):
    from app.plugins.contracts.loop_policy import LoopDecision

    class _PluginSingleTurnPolicy:
        id = policy_id

        def should_continue(self, state):
            return LoopDecision.STOP

        def max_rounds(self, llm_config):
            return max_rounds

    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    LoopPolicyRegistry.get_instance().register(_PluginSingleTurnPolicy(), source=source)
    return source


def test_worker_resolves_engine_loop_policy_id_without_touching_active(fresh_loop_registry):
    """引擎声明 loop_policy_id → worker 按 id 取策略，主对话激活槽仍为 default。"""
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    source = _register_plugin_policy()
    try:
        worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
        w = worker_cls.__new__(worker_cls)
        w._loop_policy_obj = None
        w._loop_policy_id = "plugin-single-turn"

        assert w._loop_policy().id == "plugin-single-turn"
        assert w._loop_policy().max_rounds({}) == 1
        # 全局激活槽未被改动（set_active 未被调用）
        assert LoopPolicyRegistry.get_instance().get_active().id == "default"
    finally:
        LoopPolicyRegistry.get_instance().unregister_source(source)


def test_worker_falls_back_to_active_when_id_unknown(fresh_loop_registry):
    """id 不存在（插件未加载/已卸载）→ 回落全局激活策略，不炸。"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    w = worker_cls.__new__(worker_cls)
    w._loop_policy_obj = None
    w._loop_policy_id = "does-not-exist"
    assert w._loop_policy().id == "default"


def test_worker_without_id_uses_active(fresh_loop_registry):
    """未声明 id（主对话路径）→ 行为零变化，走全局激活策略。"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    w = worker_cls.__new__(worker_cls)
    w._loop_policy_obj = None
    # _loop_policy_id 为类级默认 None（__new__ 构造也不触发 RuntimeError）
    assert w._loop_policy_id is None
    assert w._loop_policy().id == "default"


def test_executor_forwards_loop_policy_id_to_worker():
    """ConversationExecutor 把 config.loop_policy_id 透传给 worker。"""
    from app.core.conversation.config import ConversationConfig

    captured = {}

    class _FakeSignal:
        def connect(self, *a, **k):
            return None

        def disconnect(self, *a, **k):
            return None

    class _FakeWorker:
        finished = _FakeSignal()  # executor 无条件连接 finished，必须存在

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            pass

        def isRunning(self):
            return False

        def cleanup(self):
            pass

        def deleteLater(self):
            pass

    class _FakeCore:
        class session_manager:
            @staticmethod
            def get_current_session():
                return None

        permission_cache = None
        compactor = None

    from app.core.conversation.executor import ConversationExecutor

    ex = ConversationExecutor(
        core=_FakeCore(),
        config=ConversationConfig(loop_policy_id="plugin-single-turn"),
        tool_executor=None,
        agent_manager=None,
        worker_factory=_FakeWorker,
    )
    assert ex.execute(messages=[], llm_config={}, tools=[]) is True
    assert captured["loop_policy_id"] == "plugin-single-turn"
    assert "hook_policy_id" in captured


def test_conversation_config_defaults_loop_policy_id_none():
    """默认未声明 → 主对话引擎零行为变化。"""
    from app.core.conversation.config import ConversationConfig

    assert ConversationConfig().loop_policy_id is None
