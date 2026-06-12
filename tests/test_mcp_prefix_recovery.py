# -*- coding: utf-8 -*-
"""
回归测试：MCP 工具名前缀一致性

覆盖两个相关 bug：
1) MCPClientManager.get_status() 应返回带 mcp__ 前缀的完整工具名，
   避免 LLM 从 mcp_list_servers 看到裸名后误用。
2) ToolExecutor._try_recover_mcp_prefix() 在 LLM 漏掉前缀时，
   应能根据已知 MCP 工具表唯一性补回前缀。

测试策略：
- 用 SimpleNamespace 模拟 MCPServerConnection / mcp_types.Tool，
  不需要真实 MCP 连接。
- 用 __new__ 绕过 ToolExecutor.__init__（避免启动 Qt/事件循环）。
- 用 unittest + mock 隔离被测对象。
"""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.tool_executor import ToolExecutor
from app.tools.mcp_tools import MCPClientManager


def _make_conn(server_name: str, tool_names, enabled=True, has_session=True):
    """构造一个简易的 MCP server 连接。"""
    return SimpleNamespace(
        name=server_name,
        server_type="stdio",
        enabled=enabled,
        session=MagicMock() if has_session else None,
        tools=[SimpleNamespace(name=n) for n in tool_names],
    )


# ────────────────────────────────────────────────────────────────────
# Fix 1: get_status() 返回的工具名必须带 mcp__ 前缀
# ────────────────────────────────────────────────────────────────────
class TestGetStatusPrefix(unittest.TestCase):
    def setUp(self):
        # 重置单例并绕过 __init__（避免启动后台事件循环）
        MCPClientManager._instance = None
        self.mgr = MCPClientManager.__new__(MCPClientManager)
        self.mgr._connections = {}

    def test_get_status_returns_prefixed_tool_names(self):
        self.mgr._connections["playwright"] = _make_conn(
            "playwright", ["browser_navigate", "browser_snapshot"]
        )
        self.mgr._connections["github"] = _make_conn(
            "github", ["create_issue"]
        )

        status = self.mgr.get_status()

        self.assertEqual(len(status), 2)
        by_name = {s["name"]: s for s in status}

        # 关键断言：每个工具名都必须带 mcp__{server}__ 前缀
        self.assertEqual(
            by_name["playwright"]["tools"],
            [
                "mcp__playwright__browser_navigate",
                "mcp__playwright__browser_snapshot",
            ],
        )
        self.assertEqual(
            by_name["github"]["tools"],
            ["mcp__github__create_issue"],
        )

    def test_get_status_never_returns_raw_names(self):
        """防御性断言：tools 字段中不应出现任何裸名。"""
        self.mgr._connections["playwright"] = _make_conn(
            "playwright", ["browser_navigate"]
        )
        status = self.mgr.get_status()
        tools = status[0]["tools"]
        # 反向断言：所有返回的 tool 都必须以 mcp__ 开头
        for t in tools:
            self.assertTrue(
                t.startswith("mcp__"),
                f"裸名泄漏：{t!r} 不应出现在 mcp_list_servers 的返回中",
            )


# ────────────────────────────────────────────────────────────────────
# Fix 2: ToolExecutor 防御性补全 mcp__ 前缀
# ────────────────────────────────────────────────────────────────────
class TestRecoverMcpPrefix(unittest.TestCase):
    def setUp(self):
        # 构造一个最小可用的 ToolExecutor，绕过 __init__
        self.executor = ToolExecutor.__new__(ToolExecutor)
        self.executor._builtin_tools = MagicMock()
        self.executor._builtin_tools._mcp_manager = self._build_mock_manager()

    def _build_mock_manager(self):
        mgr = MagicMock()
        mgr.TOOL_PREFIX = "mcp__"
        mgr.is_connected = True
        mgr._connections = {
            "playwright": _make_conn(
                "playwright", ["browser_navigate", "browser_snapshot"]
            ),
            "github": _make_conn("github", ["create_issue"]),
        }
        return mgr

    def test_recover_with_server_and_tool(self):
        """'playwright__browser_navigate' 应被补全为 'mcp__playwright__browser_navigate'。"""
        result = self.executor._try_recover_mcp_prefix("playwright__browser_navigate")
        self.assertEqual(result, "mcp__playwright__browser_navigate")

    def test_recover_unique_tool_name_across_servers(self):
        """只有一个 server 有 'create_issue'，应能唯一确定补全。"""
        result = self.executor._try_recover_mcp_prefix("create_issue")
        self.assertEqual(result, "mcp__github__create_issue")

    def test_already_prefixed_passes_through(self):
        """已经带前缀的工具名应原样返回。"""
        result = self.executor._try_recover_mcp_prefix("mcp__github__create_issue")
        self.assertEqual(result, "mcp__github__create_issue")

    def test_ambiguous_tool_name_returns_none(self):
        """两个 server 都有同名工具时，不应猜测，应返回 None。"""
        self.executor._builtin_tools._mcp_manager._connections["playwright"].tools.append(
            SimpleNamespace(name="create_issue")
        )
        result = self.executor._try_recover_mcp_prefix("create_issue")
        self.assertIsNone(result)

    def test_unknown_tool_returns_none(self):
        result = self.executor._try_recover_mcp_prefix("nonexistent_tool")
        self.assertIsNone(result)

    def test_disabled_server_tool_not_recovered(self):
        """被禁用的 server 的工具不应参与匹配。"""
        self.executor._builtin_tools._mcp_manager._connections[
            "playwright"
        ].enabled = False
        result = self.executor._try_recover_mcp_prefix("browser_navigate")
        self.assertIsNone(result)

    def test_disconnected_manager_returns_none(self):
        self.executor._builtin_tools._mcp_manager.is_connected = False
        result = self.executor._try_recover_mcp_prefix("browser_navigate")
        self.assertIsNone(result)

    def test_no_builtin_tools_returns_none(self):
        self.executor._builtin_tools = None
        result = self.executor._try_recover_mcp_prefix("browser_navigate")
        self.assertIsNone(result)

    def test_empty_tool_name_returns_none(self):
        result = self.executor._try_recover_mcp_prefix("")
        self.assertIsNone(result)


if __name__ == "__main__":
    # 让测试可以独立跑：cd 到项目根
    sys.exit(unittest.main(verbosity=2, exit=False).result.wasSuccessful())
