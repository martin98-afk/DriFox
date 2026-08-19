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
    "agents", "hooks", "commands", "themes", "skills",
    "mcp", "lsp", "ui", "tools", "providers", "team_templates",
}

_BUILTIN_REGISTERED: set = set()


def _reload_agents(ctx: ReloadContext) -> Any:
    """agents 分支：backend.py:2014-2028 原样迁入（含 agent 命令局部重载；hooks 重载由 reload_plugin_agents 一并完成）"""
    am = _RUNTIME.get("agent_manager")
    if am is None:
        return False
    count = am.reload_plugin_agents(ctx.plugin_name)
    try:
        reload_agent_commands()
    except Exception as e:
        logger.error(f"[builtin_reloaders] Failed to reload commands after agent change: {e}")
    return count


def _reload_hooks(ctx: ReloadContext) -> Any:
    """hooks 分支：backend.py:2030-2034 原样迁入"""
    am = _RUNTIME.get("agent_manager")
    if am is None:
        return False
    am.reload_plugin_hooks(ctx.plugin_name)
    return True


def _reload_commands(ctx: ReloadContext) -> Any:
    """commands 分支：backend.py:2036-2044 原样迁入"""
    reload_all_commands()
    return True


def _reload_themes(ctx: ReloadContext) -> Any:
    """themes 分支：backend.py:2046-2060 原样迁入"""
    from app.utils.config import update_theme_options
    from app.utils.theme_manager import theme_manager

    theme_manager.reload()
    update_theme_options()
    return True


def _reload_skills(ctx: ReloadContext) -> Any:
    """skills 分支：backend.py:2062-2069 原样迁入"""
    invalidate_skills_cache()
    return True


def _reload_mcp(ctx: ReloadContext) -> Any:
    """mcp 分支：backend.py:2071-2075 原样迁入（PluginManager 已 rescan，懒生效）"""
    return True


def _reload_lsp(ctx: ReloadContext) -> Any:
    """lsp 分支：backend.py:2077-2094 原样迁入（增量 remove + add）"""
    from app.core.lsp.lsp_manager import get_lsp_manager
    from app.plugins.managers.plugin_manager import PluginManager

    lsp_mgr = get_lsp_manager()
    lsp_mgr.remove_plugin_servers(ctx.plugin_name)
    pm = PluginManager.get_instance()
    lsp_config = pm.get_plugin_lsp_config(ctx.plugin_name)
    if lsp_config:
        count = lsp_mgr.add_plugin_servers(ctx.plugin_name, lsp_config["config"])
        return count > 0
    return True


def _reload_ui(ctx: ReloadContext) -> Any:
    """ui 分支：backend.py:2096-2105 原样迁入（先卸后载）"""
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    if ctx.plugin is None or not ctx.plugin.has_component("ui"):
        return False
    UIPluginRegistry.get_instance().reload_plugin(ctx.plugin_name, ctx.plugin.path)
    return True


def _reload_tools(ctx: ReloadContext) -> Any:
    """tools 分支：轮询 watcher 退役后的正式路径 — 全量重扫（幂等，含 enabled 过滤）"""
    from app.plugins.loaders.plugin_tool_loader import ensure_plugin_tool_watcher

    watcher = ensure_plugin_tool_watcher()
    if watcher is not None:
        watcher.scan_now()
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
    }
    for comp, fn in mapping.items():
        registry.register(comp, fn)
    _BUILTIN_REGISTERED.add(id(registry))
