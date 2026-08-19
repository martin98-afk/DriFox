# -*- coding: utf-8 -*-
"""worker 循环接入 LoopPolicy — 轮数上限与策略判定可被插件接管"""
import pytest


@pytest.fixture()
def fresh_loop_registry(monkeypatch):
    from app.plugins.builtin_runtime import DefaultLoopPolicy, ensure_builtin_runtime
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    ensure_builtin_runtime()
    reg = LoopPolicyRegistry.get_instance()
    # 保证干净基线：仅 builtin default
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