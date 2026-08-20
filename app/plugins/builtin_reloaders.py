# -*- coding: utf-8 -*-
"""内置组件 reloader — 从 backend._reload_single_plugin 8 分支原样迁入。

迁移原则（零行为变化）：函数体逐字搬运 backend.py:2014-2113 的分支逻辑，
仅做三处机械变换：
1. if component == "xxx": → def _reload_xxx(ctx):
2. result["xxx"] = ...   → return ...
3. plugin / plugin_name  → ctx.plugin / ctx.plugin_name
其余（注释、子进程调用、日志）原样保留。多组件联动（agents→hooks→commands）
放在单个 reloader 内完成，与现状一致。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.core.builtin_commands import reload_agent_commands, reload_all_commands
from app.plugins.kernel import ComponentReloaderRegistry, ReloadContext
from app.utils.utils import invalidate_skills_cache

# 本模块注册的组件全集（= kernel.KNOWN_COMPONENTS）
RELOADED_COMPONENTS = {
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

_BUILTIN_REGISTERED: set = set()


def _reload_agents(ctx: ReloadContext) -> Any:
    """agents 分支（删除路径 ctx.plugin is None → cleanup_plugin_artifacts）

    含 agent 命令局部重载（reload 路径），hooks 重载由 reload_plugin_agents 一并完成。
    删除路径：cleanup_plugin_artifacts 已包 hooks 清扫；命令清理由 _reload_commands 统一处理
    （删除分支不再调 reload_agent_commands，避免与 commands reloader 双触发，对齐旧 elif 语义：
    agents 命中后不走 commands 分支）。
    """
    am = _RUNTIME.get("agent_manager")
    if am is None:
        return False
    if ctx.plugin is None:
        # 删除路径：清理 agents + hooks + 缓存；命令清理由 _reload_commands 统一处理
        am.cleanup_plugin_artifacts(ctx.plugin_name)
        return 0
    count = am.reload_plugin_agents(ctx.plugin_name)
    try:
        reload_agent_commands()
    except Exception as e:
        logger.error(f"[builtin_reloaders] Failed to reload commands after agent change: {e}")
    return count


def _reload_hooks(ctx: ReloadContext) -> Any:
    """hooks 分支（删除路径 ctx.plugin is None → unregister_skill_hooks）

    注：agents 路径的 hooks 重载由 _reload_agents 内 cleanup_plugin_artifacts / reload_plugin_agents
    联合完成。此 reloader 处理 plugins 标记 hooks 但无 agents 的场景（hooks-only 插件）。
    """
    am = _RUNTIME.get("agent_manager")
    if am is None:
        return False
    if ctx.plugin is None:
        # 删除路径：直接反注册 hooks（cleanup_plugin_artifacts 也调用同样接口，
        # 这里单独处理 hooks-only 插件删除时调用方可能未同时触发 agents）
        hm = getattr(am, "_hook_manager", None)
        if hm is not None:
            hm.unregister_skill_hooks(ctx.plugin_name)
        return True
    am.reload_plugin_hooks(ctx.plugin_name)
    return True


def _reload_commands(ctx: ReloadContext) -> Any:
    """commands 分支：增删均走全量 reload_all_commands（与删除路径现状对齐）"""
    reload_all_commands()
    return True


def _reload_themes(ctx: ReloadContext) -> Any:
    """themes 分支"""
    from app.utils.config import update_theme_options
    from app.utils.theme_manager import theme_manager

    theme_manager.reload()
    update_theme_options()
    return True


def _reload_skills(ctx: ReloadContext) -> Any:
    """skills 分支"""
    invalidate_skills_cache()
    return True


def _reload_mcp(ctx: ReloadContext) -> Any:
    """mcp 分支（PluginManager 已 rescan，懒生效）"""
    return True


def _reload_lsp(ctx: ReloadContext) -> Any:
    """lsp 分支（删除路径 ctx.plugin is None → 只 remove，不再 add）"""
    from app.core.lsp.lsp_manager import get_lsp_manager
    from app.plugins.managers.plugin_manager import PluginManager

    lsp_mgr = get_lsp_manager()
    if ctx.plugin is None:
        # 删除路径：仅增量移除（无 add — 插件已不在）
        removed = lsp_mgr.remove_plugin_servers(ctx.plugin_name)
        return removed > 0
    lsp_mgr.remove_plugin_servers(ctx.plugin_name)
    pm = PluginManager.get_instance()
    lsp_config = pm.get_plugin_lsp_config(ctx.plugin_name)
    if lsp_config:
        count = lsp_mgr.add_plugin_servers(ctx.plugin_name, lsp_config["config"])
        return count > 0
    return True


def _reload_ui(ctx: ReloadContext) -> Any:
    """ui 分支（删除路径 ctx.plugin is None → unload_plugin）"""
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    if ctx.plugin is None:
        UIPluginRegistry.get_instance().unload_plugin(ctx.plugin_name)
        return True
    if not ctx.plugin.has_component("ui"):
        return False
    UIPluginRegistry.get_instance().reload_plugin(ctx.plugin_name, ctx.plugin.path)
    return True


def _reload_tools(ctx: ReloadContext) -> Any:
    """tools 分支：轮询 watcher 退役后的正式路径 — 全量重扫（幂等，含 enabled 过滤）

    删除路径无意义（PluginManager 已移除，watcher.scan_now 重读自然不包含该插件）。
    """
    from app.plugins.loaders.plugin_tool_loader import ensure_plugin_tool_watcher

    watcher = ensure_plugin_tool_watcher()
    if watcher is not None:
        watcher.scan_now()
        if ctx.plugin is not None:
            watcher._notify_reloaded()
        return True
    return False


def _reload_providers(ctx: ReloadContext) -> Any:
    """providers 分支：同 tools，全量重扫（幂等，user 覆盖 system 语义在 loader 内）"""
    from app.plugins.loaders.provider_loader import ensure_provider_watcher

    watcher = ensure_provider_watcher()
    if watcher is not None:
        watcher.scan_now()
        return True
    return False


def _reload_team_templates(ctx: ReloadContext) -> Any:
    """team_templates 分支：懒加载，无缓存需失效 — 记日志即成功"""
    logger.debug(f"[builtin_reloaders] team_templates for '{ctx.plugin_name}' (lazy)")
    return True


def _reload_model_adapters(ctx: ReloadContext) -> Any:
    """model_adapters 分支：全量重扫运行时组件（幂等，删除路径靠重扫自然清理）"""
    try:
        from app.plugins.loaders.runtime_component_loader import ensure_model_adapter_watcher

        watcher = ensure_model_adapter_watcher()
        if watcher is not None:
            watcher.scan_now()
            return True
    except Exception as e:
        logger.warning(f"[builtin_reloaders] model_adapters 重载失败: {e}")
    return False


def _reload_loop_policies(ctx: ReloadContext) -> Any:
    """loop_policies 分支：同 model_adapters"""
    try:
        from app.plugins.loaders.runtime_component_loader import ensure_loop_policy_watcher

        watcher = ensure_loop_policy_watcher()
        if watcher is not None:
            watcher.scan_now()
            return True
    except Exception as e:
        logger.warning(f"[builtin_reloaders] loop_policies 重载失败: {e}")
    return False


def _reload_storages(ctx: ReloadContext) -> Any:
    """storages 分支：同 model_adapters"""
    try:
        from app.plugins.loaders.runtime_component_loader import ensure_storage_watcher

        watcher = ensure_storage_watcher()
        if watcher is not None:
            watcher.scan_now()
            return True
    except Exception as e:
        logger.warning(f"[builtin_reloaders] storages 重载失败: {e}")
    return False


def _reload_serializers(ctx: ReloadContext) -> Any:
    """serializers 分支：同 model_adapters"""
    try:
        from app.plugins.loaders.runtime_component_loader import ensure_serializer_watcher

        watcher = ensure_serializer_watcher()
        if watcher is not None:
            watcher.scan_now()
            return True
    except Exception as e:
        logger.warning(f"[builtin_reloaders] serializers 重载失败: {e}")
    return False


def _purge_gateway_plugin_modules(plugin_name: str) -> None:
    """从 sys.modules 摘除 gateway runtime loader 加载的模块及其依赖引用

    runtime loader 以 `drifox_rt_gateways_{plugin_name}_{py.stem}` 命名并常驻
    sys.modules。卸载/热更新若不清理，旧模块（及其 import 的第三方 SDK 等依赖）
    会残留在进程中，导致依赖无法去除、热更新代码不生效。
    """
    import gc
    import sys

    prefix = f"drifox_rt_gateways_{plugin_name}"
    removed = [
        m
        for m in list(sys.modules.keys())
        if m == prefix or m.startswith(prefix + "_") or m.startswith(prefix + ".")
    ]
    for m in removed:
        sys.modules.pop(m, None)
    if removed:
        import importlib

        importlib.invalidate_caches()
        gc.collect()
        logger.debug(
            f"[builtin_reloaders] 已清理 gateway 模块 {len(removed)} 个: {plugin_name}"
        )


def _reload_gateways(ctx: ReloadContext) -> Any:
    """gateways 分支

    正确顺序（解决卸载依赖残留 + 热更新不生效）：
    1. 关闭该插件的 gateway 平台（stop 连接 + 摘除 manager._adapters 实例）
    2. 清理 sys.modules 中 runtime loader 加载的模块引用
    3. 删除路径(ctx.plugin is None)：scan_now 自然 unregister_source 完成卸载
       更新路径(ctx.plugin 非 None)：scan_now 重新注册最新 def → 用新
       adapter_factory 重建 adapter（此前在运行则重启），使热更新生效
    """
    try:
        from app.gateway.manager import get_platform_manager
        from app.plugins.loaders.runtime_component_loader import ensure_gateway_watcher

        # 1. 先关闭该插件的 gateway（卸载/热更新都应先断连）
        mgr = get_platform_manager()
        if mgr is not None:
            mgr.stop_plugin_platforms(ctx.plugin_name)

        # 2. 清理 module 引用（彻底去除依赖）
        _purge_gateway_plugin_modules(ctx.plugin_name)

        # 3. 全量重扫：删除路径自然 unregister；更新路径重新注册最新 def
        watcher = ensure_gateway_watcher()
        if watcher is not None:
            watcher.scan_now()

        # 4. 更新路径：用新 def 重建 adapter（热更新生效）
        if ctx.plugin is not None and mgr is not None:
            mgr.rebuild_plugin_platforms(ctx.plugin_name, restart_if_running=True)
        return True
    except Exception as e:
        logger.warning(f"[builtin_reloaders] gateways 重载失败: {e}")
    return False


# 运行时句柄：backend 初始化后注入（避免循环 import — reloader 不能 import backend）
_RUNTIME: dict = {"agent_manager": None}


def bind_runtime(agent_manager: Any) -> None:
    """backend.initialize 时调用，注入 AgentManager 引用"""
    _RUNTIME["agent_manager"] = agent_manager


def register_builtin_reloaders(registry: ComponentReloaderRegistry) -> None:
    """注册全部内置 reloader（按 registry 幂等 — 同 registry 二次调用跳过，不同 registry 各自注册）"""
    global _BUILTIN_REGISTERED
    if id(registry) in _BUILTIN_REGISTERED:
        return
    mapping = {
        "agents": _reload_agents,
        "hooks": _reload_hooks,
        "commands": _reload_commands,
        "themes": _reload_themes,
        "skills": _reload_skills,
        "mcp": _reload_mcp,
        "lsp": _reload_lsp,
        "ui": _reload_ui,
        "tools": _reload_tools,
        "providers": _reload_providers,
        "team_templates": _reload_team_templates,
        "model_adapters": _reload_model_adapters,
        "loop_policies": _reload_loop_policies,
        "storages": _reload_storages,
        "serializers": _reload_serializers,
        "gateways": _reload_gateways,
    }
    for comp, fn in mapping.items():
        registry.register(comp, fn)
    _BUILTIN_REGISTERED.add(id(registry))
