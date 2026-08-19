# -*- coding: utf-8 -*-
"""LoopPolicy：默认策略语义（与现有 worker 行为逐点对应）+ 注册表激活"""

import pytest

from app.plugins.contracts.loop_policy import LoopDecision, LoopState


@pytest.fixture()
def fresh_registry(monkeypatch):
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry
    from app.plugins.builtin_runtime import DefaultLoopPolicy

    reg = LoopPolicyRegistry()
    reg.register(DefaultLoopPolicy(), source="builtin")
    monkeypatch.setattr(LoopPolicyRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


def test_default_tool_calls_continue(fresh_registry):
    """有工具调用 → 继续（对应现有 while 循环自然继续）"""
    p = fresh_registry.get_active()
    assert p.should_continue(LoopState(tool_calls_found=True)) is LoopDecision.CONTINUE


def test_default_stop_hook_injected_continue(fresh_registry):
    """Stop hook 注入消息 → 续命一轮（对应 _stop_hook_active 单次放行）"""
    p = fresh_registry.get_active()
    assert p.should_continue(LoopState(stop_hook_injected=True)) is LoopDecision.CONTINUE


def test_default_plain_stop(fresh_registry):
    """无工具无注入 → 停止（对应现有自然完成路径）"""
    p = fresh_registry.get_active()
    assert p.should_continue(LoopState()) is LoopDecision.STOP


def test_default_repetitive_loop_continue(fresh_registry):
    """重复工具调用检测 → CONTINUE=清理后继续（现有静默清理行为）"""
    p = fresh_registry.get_active()
    assert p.should_continue(LoopState(repetitive_loop_detected=True)) is LoopDecision.CONTINUE


def test_default_max_rounds_unlimited(fresh_registry):
    """默认不限轮数（现有 while 无上限），配置键可设上限"""
    p = fresh_registry.get_active()
    assert p.max_rounds({}) is None
    assert p.max_rounds({"最大循环轮数": 5}) == 5


def test_set_active_and_fallback(fresh_registry):
    class _Minimal:
        id = "minimal"

        def should_continue(self, state):
            return LoopDecision.STOP

        def max_rounds(self, llm_config):
            return 1

    fresh_registry.register(_Minimal(), source="plugin:demo")
    assert fresh_registry.set_active("minimal") is True
    assert fresh_registry.get_active().id == "minimal"
    fresh_registry.unregister_source("plugin:demo")
    assert fresh_registry.get_active().id == "default"  # 卸载后回落默认
    assert fresh_registry.set_active("no-such") is False
