# -*- coding: utf-8 -*-
"""kernel 组件常量与 reloader 注册表测试（不触碰真实插件目录）"""

from app.plugins import kernel
from app.plugins.kernel import ComponentReloaderRegistry, ReloadContext, get_reloader_registry


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


def test_reloader_registry_dispatch():
    reg = ComponentReloaderRegistry()
    calls = []

    reg.register("tools", lambda ctx: calls.append((ctx.plugin_name, ctx.component)) or True)

    ctx = ReloadContext(plugin_name="my-plugin", plugin=None, component="tools", is_new_plugin=False)
    assert reg.reload(ctx) is True
    assert calls == [("my-plugin", "tools")]


def test_reloader_registry_unknown_component():
    reg = ComponentReloaderRegistry()
    ctx = ReloadContext(plugin_name="p", plugin=None, component="nope", is_new_plugin=False)
    assert reg.reload(ctx) is False  # 未注册组件静默跳过


def test_reloader_registry_duplicate_overwrite():
    reg = ComponentReloaderRegistry()
    reg.register("tools", lambda ctx: True)
    reg.register("tools", lambda ctx: False)  # 后注册覆盖（插件热重载 reloader 本身）
    ctx = ReloadContext(plugin_name="p", plugin=None, component="tools", is_new_plugin=False)
    assert reg.reload(ctx) is False


def test_global_registry_singleton():
    assert get_reloader_registry() is get_reloader_registry()
