# -*- coding: utf-8 -*-
"""kernel 组件常量与 reloader 注册表测试（不触碰真实插件目录）"""

from app.plugins import kernel
from app.plugins.kernel import ComponentReloaderRegistry, ReloadContext, get_reloader_registry


def test_known_components_complete():
    """组件常量与 KNOWN_COMPONENTS 一致（缺一类 watchfiles 就识别不到）"""
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
        "model_adapters",
        "loop_policies",
        "storages",
        "serializers",
        "gateways",
    }
    assert kernel.KNOWN_COMPONENTS == expected


def test_root_file_components():
    """根目录关键文件映射 — 必须含 .mcp.json/.lsp.json/.drifox-plugin

    .drifox-plugin 是清单目录（plugin.json），其变更意味着组件清单可能增删，
    映射到 sentinel "__manifest__" 触发 backend 全组件重载（Task 8）。
    """
    assert kernel.ROOT_FILE_COMPONENTS[".mcp.json"] == "mcp"
    assert kernel.ROOT_FILE_COMPONENTS[".lsp.json"] == "lsp"
    assert kernel.ROOT_FILE_COMPONENTS[".drifox-plugin"] == "__manifest__"


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


def test_plugin_manager_uses_kernel_constants():
    """plugin_manager 目录探测不再自带组件名清单，统一引用 kernel.KNOWN_COMPONENTS

    万物为插件：新增组件类型时 plugin_manager 应通过 KNOWN_COMPONENTS 自动感知，
    而非在 _scan_plugins/_scan_one_plugin_dir 内硬编码探测组件清单。
    """
    import inspect
    from app.plugins.managers import plugin_manager as pm_mod

    src = inspect.getsource(pm_mod)
    assert ("from app.plugins.kernel import" in src) or ("from app.plugins import kernel" in src), (
        "plugin_manager 必须显式 import kernel 常量以保持探测规则单一事实源"
    )


def test_component_order_explicit_tuple():
    """COMPONENT_ORDER 必须为显式 tuple，避免 set 遍历时序不确定。

    backend 的 reload_plugin_subsystems 增量段按它排序遍历——若漏掉类型，
    必出现某组件在删除清理时被跳过。顺序对齐旧 backend._COMPONENT_ORDER dict。
    """
    assert isinstance(kernel.COMPONENT_ORDER, tuple), "COMPONENT_ORDER 必须是 tuple 类型，避免 set 顺序不确定性"
    # 包含全部组件（与 KNOWN_COMPONENTS 一致 — 单源真理）
    assert set(kernel.COMPONENT_ORDER) == kernel.KNOWN_COMPONENTS
    # 不重复
    assert len(kernel.COMPONENT_ORDER) == len(set(kernel.COMPONENT_ORDER))
    # 顺序：agents → hooks → commands → themes → skills → mcp → lsp → ui → tools → providers → team_templates → model_adapters → loop_policies → storages → serializers → gateways
    assert kernel.COMPONENT_ORDER == (
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
        "model_adapters",
        "loop_policies",
        "storages",
        "serializers",
        "gateways",
    )
