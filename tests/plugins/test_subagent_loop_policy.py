# -*- coding: utf-8 -*-
"""子智能体循环策略 — 轮数限制 + 最后总结机制（从 subagent_worker 硬编码拆出）

覆盖：
- SubagentLoopPolicy 语义（should_continue / max_rounds 兜底 30 / final_summary_prompt）
- 注册表 scope="subagent" 独立激活槽
- subagent_worker 接入：agent.steps 优先、策略兜底、异常回退
"""

import pytest

from app.plugins.contracts.loop_policy import LoopDecision, LoopState


@pytest.fixture()
def fresh_registry(monkeypatch):
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    warmup_runtime_components()
    reg = LoopPolicyRegistry.get_instance()
    yield reg


# ===== SubagentLoopPolicy 策略语义 =====


def test_policy_semantics(fresh_registry):
    """scope 声明 + 继续/停止判定（与原硬编码行为等价）"""
    from plugins.system.loop_policies.subagent import SubagentLoopPolicy

    p = SubagentLoopPolicy()
    assert p.id == "subagent"
    assert p.scope == "subagent"
    assert p.should_continue(LoopState(tool_calls_found=True)) is LoopDecision.CONTINUE
    assert p.should_continue(LoopState()) is LoopDecision.STOP
    # 防御性对齐 default（当前 worker 不触发该标志）
    assert p.should_continue(LoopState(repetitive_loop_detected=True)) is LoopDecision.CONTINUE


def test_max_rounds_default_and_config(fresh_registry):
    """默认 30（与原 max_iterations=30 等价）；配置键可调"""
    from plugins.system.loop_policies.subagent import SubagentLoopPolicy

    p = SubagentLoopPolicy()
    assert p.max_rounds({}) == 30
    assert p.max_rounds(None) == 30
    assert p.max_rounds({"子智能体最大轮数": 5}) == 5
    assert p.max_rounds({"子智能体最大轮数": "bad"}) == 30  # 非法值回退


def test_final_summary_prompt_content(fresh_registry):
    """总结提示词与原 _build_final_summary_prompt 内容等价"""
    from plugins.system.loop_policies.subagent import SubagentLoopPolicy

    prompt = SubagentLoopPolicy().final_summary_prompt()
    assert "总结当前执行结果" in prompt
    assert "已完成的工作" in prompt
    assert "直接输出总结内容" in prompt


def test_registry_subagent_slot_default(fresh_registry):
    """warmup 后 subagent 域默认激活 subagent 策略，main 域不受影响"""
    assert fresh_registry.get_active("subagent").id == "subagent"
    assert fresh_registry.get_active("main").id == "default"
    assert fresh_registry.get_active().id == "default"  # 缺省 scope=main


# ===== subagent_worker 接入 =====


def _make_worker(**attrs):
    mod = pytest.importorskip("app.core.workers.subagent_worker")
    w = mod.SubAgentExecutor.__new__(mod.SubAgentExecutor)
    w._loop_policy_obj = None
    w.llm_config = {}
    w.max_iterations = None
    for k, v in attrs.items():
        setattr(w, k, v)
    return w


def test_worker_resolve_round_limit_steps_priority(fresh_registry):
    """agent.steps（max_iterations）显式声明优先于策略"""
    assert _make_worker(max_iterations=7)._resolve_round_limit() == 7


def test_worker_resolve_round_limit_policy_fallback(fresh_registry):
    """未声明 steps 时走激活 subagent 策略（默认 30）"""
    assert _make_worker()._resolve_round_limit() == 30


def test_worker_resolve_round_limit_policy_exception(fresh_registry):
    """策略 max_rounds 异常时回退 30，不炸"""
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    class _Boom:
        id = "boom-sub"
        scope = "subagent"

        def should_continue(self, state):
            return LoopDecision.STOP

        def max_rounds(self, llm_config):
            raise RuntimeError("boom from max_rounds")

    LoopPolicyRegistry.get_instance().register(_Boom(), source="plugin:test-boom-sub")
    try:
        assert LoopPolicyRegistry.get_instance().set_active("boom-sub", "subagent")
        assert _make_worker()._resolve_round_limit() == 30
    finally:
        LoopPolicyRegistry.get_instance().unregister_source("plugin:test-boom-sub")


def test_worker_final_summary_from_policy(fresh_registry):
    """激活策略提供总结提示词"""
    prompt = _make_worker()._final_summary_prompt()
    assert "总结当前执行结果" in prompt


def test_worker_final_summary_fallback_constant(fresh_registry):
    """策略无 final_summary_prompt 时回退模块级内置文案（与原文案等价）"""
    import app.core.workers.subagent_worker as sw
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    class _Bare:
        """只实现协议必需方法，不带 final_summary_prompt"""

        id = "bare-sub"
        scope = "subagent"

        def should_continue(self, state):
            return LoopDecision.STOP

        def max_rounds(self, llm_config):
            return 3

    LoopPolicyRegistry.get_instance().register(_Bare(), source="plugin:test-bare-sub")
    try:
        assert LoopPolicyRegistry.get_instance().set_active("bare-sub", "subagent")
        w = _make_worker()
        assert w._final_summary_prompt() == sw._FALLBACK_FINAL_SUMMARY_PROMPT
        assert "总结当前执行结果" in sw._FALLBACK_FINAL_SUMMARY_PROMPT
    finally:
        LoopPolicyRegistry.get_instance().unregister_source("plugin:test-bare-sub")
