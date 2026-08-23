# -*- coding: utf-8 -*-
"""LoopPolicy：默认策略语义（与现有 worker 行为逐点对应）+ 注册表激活"""

import pytest

from app.plugins.contracts.loop_policy import LoopDecision, LoopState


@pytest.fixture()
def fresh_registry(monkeypatch):
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry
    from plugins.system.loop_policies.default import DefaultLoopPolicy

    reg = LoopPolicyRegistry()
    reg.register(DefaultLoopPolicy(), source="plugin:system")
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


# ===== scope 分组（v2：主智能体/子智能体独立激活槽） =====


def test_scope_grouped_defaults(fresh_registry):
    """subagent 域默认激活 subagent 策略；main 域默认 default；两槽互不影响"""
    from plugins.system.loop_policies.subagent import SubagentLoopPolicy

    fresh_registry.register(SubagentLoopPolicy(), source="plugin:system")
    assert fresh_registry.get_active("main").id == "default"
    assert fresh_registry.get_active("subagent").id == "subagent"
    assert fresh_registry.get_active().id == "default"  # 缺省 scope=main（向后兼容）


def test_set_active_auto_routes_to_policy_scope(fresh_registry):
    """set_active 不带 scope 时按策略注册的 scope 归位：子域策略激活不影响主域槽"""
    from plugins.system.loop_policies.subagent import SubagentLoopPolicy

    class _CustomSub:
        id = "custom-sub"
        scope = "subagent"

        def should_continue(self, state):
            return LoopDecision.STOP

        def max_rounds(self, llm_config):
            return 5

    fresh_registry.register(SubagentLoopPolicy(), source="plugin:system")
    fresh_registry.register(_CustomSub(), source="plugin:demo")
    assert fresh_registry.set_active("custom-sub") is True  # 自动归入 subagent 槽
    assert fresh_registry.get_active("subagent").id == "custom-sub"
    assert fresh_registry.get_active("main").id == "default"  # main 槽不受影响
    # 卸载后 subagent 槽独立回落该域默认
    fresh_registry.unregister_source("plugin:demo")
    assert fresh_registry.get_active("subagent").id == "subagent"


def test_old_policy_without_scope_defaults_main(fresh_registry):
    """未声明 scope 的旧策略按 main 兜底（向后兼容旧插件）"""

    class _Legacy:
        id = "legacy"

        def should_continue(self, state):
            return LoopDecision.STOP

        def max_rounds(self, llm_config):
            return 1

    fresh_registry.register(_Legacy(), source="plugin:legacy")
    assert fresh_registry.set_active("legacy") is True
    assert fresh_registry.get_active("main").id == "legacy"
    assert fresh_registry.get_active("subagent").id in ("subagent", "default")  # 子域不受污染
    fresh_registry.unregister_source("plugin:legacy")
