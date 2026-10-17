# -*- coding: utf-8 -*-
"""回归测试：MCP 工具调用必须在 lifespan Task 内执行。

背景（根因）：
- 用户反馈「Notion 连上过一会就断」。
- 实测（.tmp_notion_ab.py 对照实验）：
    A. 同 Task 内 await session.call_tool      → 9.8s 成功，返回 content
    B. 跨 Task 走 MCPClientManager.call_tool_sync → 挂死到超时
- 根因：ClientSession 的 _receive_loop 及其底层 anyio memory stream 由
  lifespan Task 创建并持有。call_tool_sync 经 run_coroutine_threadsafe
  把 _call_tool 丢进一个新 Task，往 session._write_stream 写请求后
  永远等不到响应投递，只能等外部 timeout。
- 挂死的调用还连带污染 lifespan TaskGroup，最终表现为运行中断开。

修复：session 操作全部收敛到 lifespan Task。_call_tool 改为把协程
提交给 lifespan Task 内的调用循环执行，自己只 await future。

本测试锁定「call_tool_sync 不在新 Task 里直接碰 session」这一行为。
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from PySide6.QtWidgets import QApplication


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return _ensure_qapp()


def _make_manager():
    from app.tools.mcp_tools import MCPClientManager

    mgr = MCPClientManager.__new__(MCPClientManager)
    mgr._connections = {}
    mgr._lock = __import__("threading").Lock()
    mgr._busy_names = set()
    mgr._busy_lock = __import__("threading").Lock()
    mgr._loop = None
    mgr._thread = None
    return mgr


class TestCallToolRunsInLifespanTask:
    """call_tool_sync 必须把调用交给 lifespan Task 执行"""

    def test_call_tool_submits_to_lifespan_task(self):
        """_call_tool 不再直接 await session，而是提交到 lifespan Task"""
        import ast

        repo = Path(__file__).resolve().parent.parent.parent
        src = (repo / "app" / "tools" / "mcp_tools.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != "MCPClientManager":
                continue
            for method in node.body:
                if not isinstance(method, ast.FunctionDef) or method.name != "_call_tool":
                    continue
                body = ast.get_source_segment(src, method) or ""
                # 修复后：调用经 conn 上的提交通道进入 lifespan Task
                assert "submit" in body or "run_in_lifespan" in body, (
                    "_call_tool 应经提交通道在 lifespan Task 内执行 session 调用"
                )
                # 不得再直接 await session.call_tool（跨 Task 的根因）
                assert "await conn.session.call_tool" not in body, (
                    "_call_tool 不得在新 Task 内直接 await conn.session.call_tool"
                )

    def test_lifespan_has_call_loop(self):
        """lifespan 内必须有消费调用队列的循环"""
        import ast

        repo = Path(__file__).resolve().parent.parent.parent
        src = (repo / "app" / "tools" / "mcp_tools.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != "MCPServerConnection":
                continue
            for method in node.body:
                if isinstance(method, ast.FunctionDef) and "submit" in method.name:
                    found = True
        assert found, "MCPServerConnection 应提供调用提交方法"

    def test_connection_has_call_queue(self):
        """连接对象持有调用队列"""
        from app.tools.mcp_tools import MCPServerConnection

        conn = MCPServerConnection.__new__(MCPServerConnection)
        # 构造后即应有队列属性（__init__ 或属性默认值提供）
        conn.__init__("demo", {"name": "demo", "type": "http", "url": "https://x/mcp"})
        assert hasattr(conn, "_call_queue"), "连接对象应持有 _call_queue"


class TestCallToolErrorPaths:
    """调用链路的错误处理不回归"""

    def test_unconnected_server_returns_error(self):
        mgr = _make_manager()

        async def run():
            return await mgr._call_tool("mcp__ghost__foo", {})

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(run())
        finally:
            loop.close()

        assert res.success is False
        assert "未连接" in res.error

    def test_invalid_tool_name(self):
        mgr = _make_manager()
        # 造一个已连接但工具名非法的连接
        from app.tools.mcp_tools import MCPServerConnection

        conn = MCPServerConnection("demo", {"name": "demo", "type": "http", "url": "https://x"})
        conn.session = MagicMock()
        mgr._connections["demo"] = conn

        async def run():
            return await mgr._call_tool("mcp__demo__not_valid", {})

        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(run())
        finally:
            loop.close()

        assert res.success is False


class TestLifespanErrorLogging:
    """掉线日志必须带真实原因（anyio TaskGroup 包装问题）"""

    def test_error_log_extracts_real_cause(self):
        """运行中断开日志不得只写 'unhandled errors in a TaskGroup'"""
        repo = Path(__file__).resolve().parent.parent.parent
        src = (repo / "app" / "tools" / "mcp_tools.py").read_text(encoding="utf-8")
        assert "运行中断开: {type(actual).__name__}: {actual}" in src, (
            "掉线日志应先 _extract_real_error 再输出，否则 TaskGroup 包装消息会淹没真实原因"
        )
        assert 'logger.warning(f"[MCP] 服务器 \'{conn.name}\' 运行中断开: {e}")' not in src
