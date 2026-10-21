# -*- coding: utf-8 -*-
"""审计日志去重：防止 _execute_registered_tool / tool_registry_api 的瞬时池刷屏。

回归背景：host 无关的调用点（工具实现 / hook / context tier）每次现建池，
而审计去重原本挂在池实例上（_audited 每池独立）→ 每次调用都记一行，
实测 tool.read 满屏 [CapabilityAudit] plugin=(builtin)。

两处修复：
1. registry 缓存 host 无关的池（同 plugin/surface/declared 复用）
2. 审计去重提到 registry，按 (plugin, surface, cap_id) 全局去重
"""

import pytest
from loguru import logger


@pytest.fixture(autouse=True)
def _fresh():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def test_hostless_pool_is_cached():
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("tool")
    reg = CapabilityRegistry.get_instance()
    a = reg.build_pool(surface="tool", host=None, plugin="demo", declared={"tool.read"})
    b = reg.build_pool(surface="tool", host=None, plugin="demo", declared={"tool.read"})
    assert a is b


def test_hostless_pool_differs_by_plugin():
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("tool")
    reg = CapabilityRegistry.get_instance()
    a = reg.build_pool(surface="tool", host=None, plugin="p1", declared={"tool.read"})
    b = reg.build_pool(surface="tool", host=None, plugin="p2", declared={"tool.read"})
    assert a is not b


def test_pool_with_host_is_never_cached():
    """有 host 的池必须每次新建（host 是窗口引用，缓存会串窗口）。"""
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("tool")
    reg = CapabilityRegistry.get_instance()
    a = reg.build_pool(surface="tool", host=object(), plugin="demo", declared={"tool.read"})
    b = reg.build_pool(surface="tool", host=object(), plugin="demo", declared={"tool.read"})
    assert a is not b


def test_audit_logged_once_across_pools():
    """同一 (plugin, surface, cap) 跨多个池实例只记一行。"""
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    records = []
    sink = logger.add(lambda m: records.append(str(m)), level="DEBUG")
    try:
        ensure_registered("tool")
        reg = CapabilityRegistry.get_instance()
        for _ in range(5):
            pool = reg.build_pool(surface="tool", host=None, plugin="demo", declared={"tool.read"})
            pool["tool.read"]
    finally:
        logger.remove(sink)
    audits = [r for r in records if "[CapabilityAudit]" in r and "cap=tool.read" in r]
    assert len(audits) == 1, f"预期 1 行，实际 {len(audits)}"


def test_tool_registry_api_repeated_call_is_quiet():
    """tool_registry_api 反复调用不刷屏（回归守卫）。"""
    from app.plugins.sdk_tool_registry import tool_registry_api

    records = []
    sink = logger.add(lambda m: records.append(str(m)), level="DEBUG")
    try:
        for _ in range(10):
            api = tool_registry_api("read")
            assert api is not None
    finally:
        logger.remove(sink)
    audits = [r for r in records if "[CapabilityAudit]" in r and "cap=tool.read" in r]
    assert len(audits) <= 1, f"反复调用刷屏: {len(audits)} 行"


def test_audit_attribution_uses_real_plugin_name():
    """日志里的 plugin= 必须是真实插件名，而不是 (builtin)。"""
    import sys
    from pathlib import Path

    fake = {
        "__file__": str(Path(".").resolve() / "plugins" / "demo_plug" / "tools" / "x.py")
    }
    from app.plugins.sdk_tool_registry import _caller_plugin

    assert _caller_plugin(fake) == "demo_plug"
    assert _caller_plugin(None) == ""
    assert _caller_plugin({}) == ""
