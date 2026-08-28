# -*- coding: utf-8 -*-
"""logging_setup 路由逻辑测试。

只测纯函数与路由表结构，不做真实文件 IO（避免污染全局 loguru 状态）。
"""

from app.core.logging_setup import LOG_ROUTES, _match_prefix, make_module_filter


def test_match_prefix_exact():
    """前缀本身精确命中。"""
    assert _match_prefix("app.gateway", ("app.gateway",))


def test_match_prefix_submodule():
    """子模块按路径段命中。"""
    assert _match_prefix("app.gateway.manager", ("app.gateway",))


def test_match_prefix_no_substring_false_positive():
    """子串不误命中：gateway_service 不是 gateway 子模块。"""
    assert not _match_prefix("app.core.gateway_service", ("app.core.gateway",))


def test_match_prefix_multiple_prefixes():
    """任一前缀命中即通过。"""
    assert _match_prefix("app.core.lsp.client", ("app.core.team", "app.core.lsp"))


def test_route_filter_include_exclude():
    """tools.log 命中 app.tools 但排除 app.tools.mcp_tools。"""
    f = make_module_filter(("app.tools",), ("app.tools.mcp_tools",))
    assert f({"name": "app.tools.registry"})
    assert not f({"name": "app.tools.mcp_tools"})


def test_route_filter_exclude_wins_over_include():
    """排除优先于包含：排除前缀同时出现在包含列表时仍被拒绝。"""
    f = make_module_filter(("app.tools", "app.tools.mcp_tools"), ("app.tools.mcp_tools",))
    assert not f({"name": "app.tools.mcp_tools"})


def test_mcp_prefix_excluded_from_tools():
    """mcp.log 的包含前缀必须被 tools.log 排除，保证二者互斥。"""
    mcp_includes = set()
    tools_excludes = set()
    for file_name, include, exclude in LOG_ROUTES:
        if file_name == "mcp.log":
            mcp_includes.update(include)
        elif file_name == "tools.log":
            tools_excludes.update(exclude)
    assert mcp_includes <= tools_excludes


def test_routes_structure_valid():
    """路由表结构有效性：文件名合法、包含前缀非空。"""
    file_names = set()
    for file_name, include, _exclude in LOG_ROUTES:
        assert file_name.endswith(".log")
        assert include, f"{file_name} 的包含前缀不能为空"
        file_names.add(file_name)
    assert "all.log" not in file_names, "all.log 是兜底 sink，不应出现在路由表中"


def test_key_modules_routed():
    """关键子系统模块均应命中对应分文件。"""
    expectations = {
        "app.core.backend": "llm.log",
        "app.gateway.manager": "gateway.log",
        "app.tools.mcp_tools": "mcp.log",
        "app.core.lsp.lsp_client": "lsp.log",
        "app.core.tool_executor": "tools.log",
        "plugins.system.tools.terminal_tools": "plugins.log",
        "app.main_widget": "ui.log",
        "app.core.team_manager": "team.log",
        "app.core.store.session_store": "store.log",
    }
    for module_name, expected_file in expectations.items():
        routed = [
            file_name
            for file_name, include, exclude in LOG_ROUTES
            if make_module_filter(include, exclude)({"name": module_name})
        ]
        assert expected_file in routed, f"{module_name} 未命中 {expected_file}（实际: {routed}）"
