# -*- coding: utf-8 -*-
"""${LOCAL_MCP} 占位符解析单测。

Run: pytest tests/plugins/test_local_mcp_placeholder.py -v
"""
from app.tools.mcp_tools import _resolve_plugin_paths


def test_local_mcp_placeholder_replaces_url():
    cfg = _resolve_plugin_paths({"url": "${LOCAL_MCP}/browser", "type": "http"})
    assert cfg["url"].startswith("http://127.0.0.1:")
    assert cfg["url"].endswith("/mcp/browser")


def test_local_mcp_placeholder_absent_is_noop():
    cfg = _resolve_plugin_paths({"url": "https://example.com/mcp"})
    assert cfg["url"] == "https://example.com/mcp"
