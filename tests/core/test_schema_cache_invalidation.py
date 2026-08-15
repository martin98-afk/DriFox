# -*- coding: utf-8 -*-
"""
Schema 缓存失效 / 深拷贝隔离 / team_only 过滤测试

覆盖（T2 计划 P6 + P7 + T14 补充 2）：
P6 — 缓存失效：
- 注册新工具 → get_builtin_tools_schema 立即含（registry version 变化驱动缓存失效）
- 缓存命中（无变化）→ 内容一致
补充 2 — 深拷贝隔离：
- 未命中路径返回深拷贝：改返回值 description 后二次调用不变（不污染 registry 与缓存）
P7 — team_only 过滤：
- 注册 team_only 工具 → AgentManager.get_agent_tools_schema 非团队场景过滤（LLM 看不到）
- registry.team_only_tools() 正确列出

运行: python -m pytest tests/core/test_schema_cache_invalidation.py -v
"""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.tools as tools_mod
from app.tools import get_builtin_tools_schema
from app.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def fresh_state():
    """重置 registry + 清空 schema 缓存（模块级，测试间防污染）"""
    ToolRegistry.reset_instance()
    tools_mod._CACHE_RESULT = None
    tools_mod._CACHE_VERSION = -1
    tools_mod._CACHE_TIMESTAMP = 0.0
    yield
    ToolRegistry.reset_instance()
    tools_mod._CACHE_RESULT = None


def _register(name, danger="safe", team_only=False, **meta):
    schema = {
        "type": "function",
        "function": {"name": name, "description": f"{name} desc",
                     "parameters": {"type": "object", "properties": {}}},
    }
    return ToolRegistry.get_instance().register(
        name, schema, impl=lambda **kw: "ok", danger=danger,
        source="plugin:test", team_only=team_only, **meta,
    )


def _schema_names(schemas):
    return {s["function"]["name"] for s in schemas}


class TestSchemaCacheInvalidation:
    """P6：缓存失效"""

    def test_new_tool_appears_immediately(self):
        """注册新工具 → schema 立即含（version 比对失效缓存）"""
        _register("cache_tool_a")
        first = get_builtin_tools_schema()
        assert "cache_tool_a" in _schema_names(first)

        # 再注册一个 → 缓存失效 → 新工具立即可见
        _register("cache_tool_b")
        second = get_builtin_tools_schema()
        assert "cache_tool_b" in _schema_names(second)

    def test_unregister_invalidates_cache(self):
        """注销工具 → schema 不再含（缓存同步失效）"""
        _register("cache_tool_del")
        assert "cache_tool_del" in _schema_names(get_builtin_tools_schema())

        reg = ToolRegistry.get_instance()
        reg.unregister("cache_tool_del")
        names = _schema_names(get_builtin_tools_schema())
        assert "cache_tool_del" not in names

    def test_cache_hit_consistent(self):
        """无变化时二次调用内容一致（缓存命中）"""
        _register("cache_tool_stable")
        s1 = get_builtin_tools_schema()
        s2 = get_builtin_tools_schema()
        assert s1 == s2


class TestDeepCopyIsolation:
    """补充 2：返回副本修改不污染 registry"""

    def test_modify_returned_desc_does_not_pollute(self):
        """改返回值 description → 二次调用不变（深拷贝隔离）"""
        _register("cache_copy_tool")
        s1 = get_builtin_tools_schema()
        target = next(s for s in s1 if s["function"]["name"] == "cache_copy_tool")
        target["function"]["description"] = "被污染的描述"

        # 二次调用：description 仍为原始值（缓存/registry 未被污染）
        s2 = get_builtin_tools_schema()
        target2 = next(s for s in s2 if s["function"]["name"] == "cache_copy_tool")
        assert target2["function"]["description"] == "cache_copy_tool desc"

    def test_modify_returned_schema_structure_isolated(self):
        """改返回值参数结构 → registry 原 schema 不受影响"""
        _register("cache_copy_struct")
        s1 = get_builtin_tools_schema()
        for s in s1:
            if s["function"]["name"] == "cache_copy_struct":
                s["function"]["parameters"]["properties"]["hacked"] = {"type": "string"}

        reg_schema = ToolRegistry.get_instance().get("cache_copy_struct").schema
        assert "hacked" not in reg_schema["function"]["parameters"]["properties"]


class TestTeamOnlyFilter:
    """P7：team_only 工具从非团队 schema 过滤"""

    def _make_agent_manager(self, monkeypatch):
        """构造最小 AgentManager（__new__ 绕过）+ 注入 fake agent"""
        from app.core.agent import AgentManager

        am = AgentManager.__new__(AgentManager)
        am._agents = {}
        am._hidden_agents = {}
        am._builtin_tools = None  # 非团队场景：无窗口团队上下文
        fake_agent = object()
        monkeypatch.setattr(am, "get_agent", lambda name: fake_agent)
        return am

    def test_team_only_filtered_for_non_team(self, monkeypatch):
        """非团队场景：team_only 工具从 schema 过滤（LLM 看不到）"""
        _register("team_tool_x", team_only=True)
        _register("normal_tool_y")

        am = self._make_agent_manager(monkeypatch)
        schemas = am.get_agent_tools_schema("any-agent", builtin_tools=None)
        names = _schema_names(schemas)
        assert "normal_tool_y" in names
        assert "team_tool_x" not in names, "非团队成员不应看到 team_only 工具"

    def test_team_only_tools_listed_in_registry(self):
        """registry.team_only_tools() 正确列出 team_only 工具"""
        _register("team_only_a", team_only=True)
        _register("team_only_b", team_only=True)
        _register("not_team_c")
        tools = ToolRegistry.get_instance().team_only_tools()
        assert "team_only_a" in tools
        assert "team_only_b" in tools
        assert "not_team_c" not in tools
