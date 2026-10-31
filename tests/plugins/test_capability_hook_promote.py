# -*- coding: utf-8 -*-
"""能力池：hook.promote —— 插件把自身 hook 提到注册列表最前（与 host 无关）。"""

from types import SimpleNamespace

import pytest

@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()

@pytest.fixture()
def shared_hooks(monkeypatch):
    """隔离 HookManager._shared_hooks（类属性全局共享）。"""
    from app.core.hooks.hook_manager import HookManager

    saved = dict(HookManager._shared_hooks)
    HookManager._shared_hooks.clear()
    yield HookManager._shared_hooks
    HookManager._shared_hooks.clear()
    HookManager._shared_hooks.update(saved)

def _pool(monkeypatch, declared):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("ui")
    reg = CapabilityRegistry.get_instance()
    host = SimpleNamespace(backend=None, _window_id="w1")
    return reg.build_pool(surface="ui", host=host, plugin="assistant_hub", declared=declared)

def test_promote_moves_rule_to_front(monkeypatch, shared_hooks):
    from app.core.hooks.hook_manager import HookMatchRule

    rule_a = HookMatchRule(matcher="primary")
    rule_a.hooks = []
    rule_a.skill_name = "system-hooks"
    rule_b = HookMatchRule(matcher="primary")
    rule_b.hooks = []
    rule_b.skill_name = "assistant_hub"
    shared_hooks["BuildSystemPrompt"] = [rule_a, rule_b]

    pool = _pool(monkeypatch, {"hook.promote"})
    promote = pool["hook.promote"]
    assert promote("assistant_hub") is True
    assert shared_hooks["BuildSystemPrompt"][0] is rule_b

def test_promote_idempotent(monkeypatch, shared_hooks):
    from app.core.hooks.hook_manager import HookMatchRule

    rule = HookMatchRule(matcher="primary")
    rule.hooks = []
    rule.skill_name = "assistant_hub"
    shared_hooks["BuildSystemPrompt"] = [rule]

    promote = _pool(monkeypatch, {"hook.promote"})["hook.promote"]
    assert promote("assistant_hub") is True
    assert promote("assistant_hub") is True
    assert shared_hooks["BuildSystemPrompt"] == [rule]

def test_promote_unknown_plugin_returns_false(monkeypatch, shared_hooks):
    from app.core.hooks.hook_manager import HookMatchRule

    rule = HookMatchRule(matcher="primary")
    rule.hooks = []
    rule.skill_name = "system-hooks"
    shared_hooks["BuildSystemPrompt"] = [rule]

    promote = _pool(monkeypatch, {"hook.promote"})["hook.promote"]
    assert promote("assistant_hub") is False
    assert shared_hooks["BuildSystemPrompt"] == [rule]

def test_promote_not_declared_denied(monkeypatch, shared_hooks):
    from app.plugins.capabilities.pool import CapabilityDenied

    pool = _pool(monkeypatch, {"window.current"})
    assert "hook.promote" not in pool
    with pytest.raises(CapabilityDenied):
        pool["hook.promote"]
