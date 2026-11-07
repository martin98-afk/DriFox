# -*- coding: utf-8 -*-
"""[bc697c93 补防] 幻觉 mcp__ 前缀工具名分发的友好错误回归测试。

触发链：LLM 幻觉生成 mcp__ 前缀工具名（schema 未注入挡不住幻觉）→ 分发分支
tool_name.startswith("mcp__") 必进 → 未配置 MCP 环境（property 守卫返回 None）
→ 修复前裸 ._mcp_manager 恒 None 且不触发 AttributeError 分支 → is_connected
抛 AttributeError；修复后返回友好 ToolResult。
"""
from types import SimpleNamespace

from app.core.tools.tool_executor import ToolExecutor


def _make_executor_with_unconfigured_mcp():
    """未配置 MCP 环境的 ToolExecutor 桩：property 语义 = None。"""
    ex = ToolExecutor.__new__(ToolExecutor)
    ex._builtin_tools = SimpleNamespace(mcp_manager=None)  # property 守卫的等价语义
    ex.is_valid = lambda: True
    return ex


class TestHallucinatedMcpDispatch:
    def test_unconfigured_mcp_returns_friendly_result(self):
        """未配置 MCP + mcp__ 前缀名 → 友好 ToolResult，非 AttributeError。"""
        ex = _make_executor_with_unconfigured_mcp()
        result = ex._execute_mcp_tool("mcp__hallucinated_server__phantom_tool", {})
        assert result is not None
        assert result.success is False
        assert "未连接" in result.error and "请先配置" in result.error

    def test_none_manager_short_circuits_before_is_connected(self):
        """None 管理器必须在 is_connected 访问前被短路（修复点回归）。"""
        ex = _make_executor_with_unconfigured_mcp()
        # 修复前：None.is_connected 抛 AttributeError（无 isinstance/str 语义）
        try:
            result = ex._execute_mcp_tool("mcp__x__y", {})
        except AttributeError:
            pytest_fail = True
        else:
            pytest_fail = False
            assert result.success is False
        assert not pytest_fail, "修复被回退：None.is_connected 重新抛 AttributeError"

    def test_configured_but_disconnected_also_friendly(self):
        """已配置但未连接（manager 非 None、is_connected=False）→ 同样友好错误。"""
        ex = ToolExecutor.__new__(ToolExecutor)
        manager = SimpleNamespace(is_connected=False)
        ex._builtin_tools = SimpleNamespace(mcp_manager=manager)
        ex.is_valid = lambda: True
        result = ex._execute_mcp_tool("mcp__s__t", {})
        assert result.success is False
        assert "未连接" in result.error
