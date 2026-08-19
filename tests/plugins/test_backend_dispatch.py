# -*- coding: utf-8 -*-
"""backend 表分派测试 — 验证 _reload_single_plugin 走 kernel 注册表而非 8 分支"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def kernel_env(monkeypatch, tmp_path):
    """隔离环境：空 PluginManager + 独立 reloader 注册表

    注入方式：monkeypatch `app.plugins.kernel._registry` 为本次 fixture 的独立 reg，
    并在该 reg 上注册内置 reloader（builtin_reloaders 可能在 import 时已注册到旧表，
    这里直接绑到 fixture 的 reg，确保 registry.reload() 命中）。
    """
    from app.plugins import builtin_reloaders, kernel as kernel_mod
    from app.plugins.kernel import ComponentReloaderRegistry, ReloadContext

    reg = ComponentReloaderRegistry()
    monkeypatch.setattr(kernel_mod, "_registry", reg)
    # 把 builtin_reloaders 注册到这个 fixture 的 reg（而非 import 期可能已注册的旧 reg）
    builtin_reloaders.register_builtin_reloaders(reg)

    fake_plugin = MagicMock()
    fake_plugin.components = {"themes": True}
    fake_plugin.has_component = lambda c: fake_plugin.components.get(c, False)
    fake_plugin.path = tmp_path

    fake_pm = MagicMock()
    fake_pm.is_initialized.return_value = True
    fake_pm.get_plugin.return_value = fake_plugin
    fake_pm.rescan_plugin = MagicMock()

    return reg, fake_pm


def test_dispatch_via_registry(kernel_env, monkeypatch):
    """经注册表分派：themes 组件 → 自定义 reloader 被调用"""
    reg, fake_pm = kernel_env
    from app.core.backend import ChatBackend

    calls = []
    reg.register("themes", lambda ctx: calls.append(ctx.component) or True)

    backend = ChatBackend.__new__(ChatBackend)  # 跳过 __init__（不做真实初始化）
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    result = backend._reload_single_plugin("p", "themes")
    assert calls == ["themes"]
    assert result["themes"] is True


def test_dispatch_deleted_plugin_triggers_cleanup_path(kernel_env, monkeypatch):
    """插件删除（get_plugin → None）时各 reloader 收到 plugin=None 真实分派

    Task 6：删除清理段并入 reloader 分派 — 删除路径不再手写清理分支，
    而是遍历该插件原有组件 → 调 registry.reload(ReloadContext(plugin=None))。
    各 reloader 自行处理 ctx.plugin is None 语义（agents→cleanup_plugin_artifacts、
    hooks-only→unregister_skill_hooks、commands→reload_all_commands、themes→reload、
    skills→invalidate_cache、ui→unload_plugin、lsp→remove_plugin_servers，
    tools/providers 删除路径下跳过）。
    """
    reg, fake_pm = kernel_env
    # 删除前 fake_plugin（含 components 字典，提供 removed_components 数据源）
    fake_plugin = fake_pm.get_plugin.return_value
    fake_plugin.components = {"themes": True, "commands": True, "skills": True}
    fake_plugin.has_component = lambda c: fake_plugin.components.get(c, False)

    # 第一次 get_plugin（拿 plugin_before）→ fake_plugin；第二次（rescan 后）→ None
    fake_pm.get_plugin.side_effect = [fake_plugin, None]

    from app.core.backend import ChatBackend

    seen = []
    reg.register("themes", lambda ctx: seen.append(("themes", ctx.plugin is None)) or True)
    reg.register("commands", lambda ctx: seen.append(("commands", ctx.plugin is None)) or True)
    reg.register("skills", lambda ctx: seen.append(("skills", ctx.plugin is None)) or True)

    backend = ChatBackend.__new__(ChatBackend)
    # 给 backend 实例补齐删除段依赖的属性（__new__ 跳过 __init__，默认无任何属性）
    backend._watcher_dedup_cache = {}
    backend._agent_manager = MagicMock()  # builtin_reloaders._RUNTIME 注入前占位（实际不被命中）
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    backend._reload_single_plugin("p", "themes")

    # 真实分派断言：themes/commands/skills reloader 都在删除路径被调用且 plugin is None
    assert ("themes", True) in seen, f"themes reloader 应在删除路径被调用，实际 seen={seen}"
    assert ("commands", True) in seen, f"commands reloader 应在删除路径被调用，实际 seen={seen}"
    assert ("skills", True) in seen, f"skills reloader 应在删除路径被调用，实际 seen={seen}"


def test_unknown_component_skipped(kernel_env, monkeypatch):
    """未知组件：注册的 bogus reloader 完全不被调用（plugin=None 删除路径也不命中）"""
    reg, fake_pm = kernel_env
    from app.core.backend import ChatBackend

    # 故意注册一个 bogus 名字的 reloader — 验证注册表 dispatch 不会误派给它
    calls = []
    reg.register("bogus", lambda ctx: calls.append(ctx.component) or True)

    backend = ChatBackend.__new__(ChatBackend)
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    # 非删除路径：plugin 不为 None（默认 fixture）
    result = backend._reload_single_plugin("p", "bogus")
    assert result.get("themes") is False
    assert calls == [], "未知组件名 'bogus' 的 reloader 不应被调用"

    # 删除路径也不命中：plugin=None 时同样跳过 bogus reloader
    fake_pm.get_plugin.return_value = None
    backend._reload_single_plugin("p", "bogus")
    assert calls == [], "删除路径也不应触发未知 reloader"


def test_agents_dispatch_marks_hooks_and_commands(kernel_env, monkeypatch):
    """agents 联动标记：分派 agents 时 hooks/commands 一并视为已处理"""
    reg, fake_pm = kernel_env
    fake_plugin = fake_pm.get_plugin.return_value
    fake_plugin.components = {"agents": True, "hooks": True, "commands": True}
    fake_plugin.has_component = lambda c: fake_plugin.components.get(c, False)

    from app.core.backend import ChatBackend

    calls = []
    reg.register("agents", lambda ctx: calls.append(ctx.component) or 3)

    backend = ChatBackend.__new__(ChatBackend)
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    result = backend._reload_single_plugin("p", "agents")
    assert calls == ["agents"]
    assert result["agents"] == 3
    assert result["hooks"] is True  # 联动标记
    assert result["commands"] is True  # 联动标记


def test_delete_path_iterates_by_component_order(kernel_env, monkeypatch):
    """删除段遍历必须按 COMPONENT_ORDER（tuple）— 防止 KNOWN_COMPONENTS set 序不确定漏清理。

    Task 6 Fix Round 2：旧代码用 set 遍历，漏遍历某组件会跳过其清理分支（如 agents 后置 commands 顺序错了，
    会让 commands reloader 误跑在 cleanup 之后导致状态漂移）。本测试断言删除段遍历序与
    kernel.COMPONENT_ORDER 一致，且所有声明组件都收到 ctx.plugin is None 的清理调用。
    """
    from app.plugins import builtin_reloaders
    from app.plugins import kernel as kernel_mod

    reg, fake_pm = kernel_env
    # 关键：禁用 builtin 内置 reloader（避免 agents→commands 等副作用污染 seen 列表）
    builtin_reloaders._BUILTIN_REGISTERED.discard(id(reg))

    fake_plugin = MagicMock()
    # 声明全部 11 类组件（含 team_templates 末尾），模拟复杂插件
    fake_plugin.components = {c: True for c in kernel_mod.COMPONENT_ORDER}
    fake_plugin.has_component = lambda c: fake_plugin.components.get(c, False)
    fake_plugin.path = MagicMock()
    # 第一次 get_plugin（取 plugin_before）→ fake_plugin；第二次（rescan 后）→ None
    fake_pm.get_plugin.side_effect = [fake_plugin, None]

    # 用本地 seen 收集所有 reloader 实际触发顺序
    seen: list[str] = []
    for comp in kernel_mod.COMPONENT_ORDER:
        reg.register(comp, lambda ctx, _c=comp: seen.append(_c) or True)

    from app.core.backend import ChatBackend

    backend = ChatBackend.__new__(ChatBackend)
    backend._watcher_dedup_cache = {}
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    backend._reload_single_plugin("p", "")  # component 空走删除段

    # 遍历序必须严格等于 COMPONENT_ORDER — 任何 set 序漂移都会让 assert 失败
    assert seen == list(kernel_mod.COMPONENT_ORDER), f"删除段遍历序应等于 COMPONENT_ORDER，实际 {seen}"
    # 所有声明组件都被处理
    assert set(seen) == kernel_mod.KNOWN_COMPONENTS
