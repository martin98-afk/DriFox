# -*- coding: utf-8 -*-
"""kernel 组件常量与 reloader 注册表测试（不触碰真实插件目录）"""

from app.plugins import kernel


def test_known_components_complete():
    """10 类组件 + team_templates 全部在册（缺一类 watchfiles 就识别不到）"""
    expected = {
        "agents",
        "hooks",
        "commands",
        "themes",
        "skills",
        "mcp",
        "lsp",
        "ui",
        "tools",
        "providers",
        "team_templates",
    }
    assert kernel.KNOWN_COMPONENTS == expected


def test_root_file_components():
    assert kernel.ROOT_FILE_COMPONENTS == {".mcp.json": "mcp", ".lsp.json": "lsp"}


def test_validate_component():
    assert kernel.validate_component("tools") is True
    assert kernel.validate_component("not-a-component") is False
