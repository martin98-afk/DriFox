# -*- coding: utf-8 -*-
"""``caps_for`` —— 插件在任意位置取能力的统一入口。

解决什么问题
-----------
后台线程 / 无 ctx 的调用点（切助手时的缓存失效、经验反思、tool_search 中转）
原先各自复制同一段样板：``ensure_registered`` + 从 ``__file__`` 推断插件名 +
``build_pool(host=None, declared={cap})``。这段样板有两个硬伤：

1. ``host=None`` 的池拿不到任何窗口状态，``window.current`` / ``session.read``
   这类能力在后台线程里必然返回空——能力建模本身不缺口，缺口在 host 的解析。
2. 六份拷贝，改口径要改六处。

这里的做法：**host 解析归宿主**。无 ctx 时按插件名从 UIPluginRegistry 找该
插件当前绑定的窗口（浮动卡 ctx 建立时登记），解析不到再退到活跃窗口；
host 相关能力若确实拿不到窗口，按其自身的空值语义返回（与原来 host=None
的行为一致），不伪造窗口。

授权仍严格按 manifest 声明裁剪（``resolve_declared``），本函数不提供任何
绕过声明的后门。
"""

from __future__ import annotations

from typing import Any, FrozenSet, Optional

from loguru import logger

# 插件名 → 该插件当前绑定的窗口（浮动卡 ctx 建立时登记）。
# 多窗口同一插件各开一张卡时记最后一个：卡片的 host 本就是"它显示所在窗口"，
# 后台线程取的是最近一次交互过的窗口，与 window.current 的活跃窗口语义一致。
_plugin_hosts: dict[str, Any] = {}


def _caller_plugin(skip: int = 1) -> str:
    """调用方的插件名（``plugins/<name>/...`` 的 ``<name>``）。

    推断不出返回空串（池记审计时显示为 ``(builtin)``）。
    """
    import inspect
    from pathlib import Path

    try:
        frame = inspect.stack()[skip]
        path = Path(frame.filename).resolve()
    except Exception:
        return ""
    parts = path.parts
    if "plugins" in parts:
        idx = parts.index("plugins") + 1
        if idx < len(parts):
            return parts[idx]
    return ""


def note_host(plugin: str, host: Any) -> None:
    """登记某插件当前绑定的窗口（供无 ctx 的后台调用点解析 host）。"""
    if plugin and host is not None:
        _plugin_hosts[plugin] = host


def forget_host(plugin: str, host: Any = None) -> None:
    """注销绑定。host 给出且与登记的不一致时不清（防误删别窗口的登记）。"""
    if host is None:
        _plugin_hosts.pop(plugin or "", None)
        return
    if _plugin_hosts.get(plugin or "") is host:
        _plugin_hosts.pop(plugin or "", None)


def reset_hosts() -> None:
    """测试用：清空全部绑定。"""
    _plugin_hosts.clear()


def _resolve_host(plugin: str, surface: str) -> Any:
    """按插件名解析 host：绑定窗口 → 活跃窗口 → surface 默认 host。

    tool surface 没有窗口概念（host = ToolExecutor），直接返回 None，
    由 tool_core 的 provider 自行从单例解析。
    """
    host = _plugin_hosts.get(plugin)
    if host is not None:
        return host
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        active = getattr(reg, "_main_widget", None)
        if active is None:
            widgets = getattr(reg, "_window_main_widgets", None) or {}
            active = next(iter(widgets.values()), None)
        return active
    except Exception:
        return None


def _registry():
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    return CapabilityRegistry.get_instance()


def _surface_of(caller: str) -> str:
    """按调用方所在目录判 surface：plugins/<name>/tools/ → tool。"""
    import inspect
    from pathlib import Path

    try:
        parts = Path(inspect.stack()[2].filename).resolve().parts
    except Exception:
        return "ui"
    if "plugins" in parts and "tools" in parts[parts.index("plugins") + 1 :]:
        return "tool"
    return "ui"


def caps_for(cap_id: str, plugin: str = "", surface: str = "") -> Any:
    """取某个能力的可调用对象（按调用方 manifest 声明裁剪）。

    Args:
        cap_id: 能力 ID（如 ``window.current``）
        plugin: 插件名；缺省从调用方文件路径推断
        surface: ``ui`` / ``tool``；缺省按调用方目录推断

    Returns:
        能力对象（通常是闭包/函数）；未授权、未注册或异常时返回 ``None``。

    与 ``ctx["capabilities"][cap_id]`` 的差别只在 host 来源：ctx 里的池用
    宿主注入的窗口，这里在无 ctx 时自行解析。权限判定走同一条
    ``resolve_declared``，不会因为走本函数就拿到声明外的能力。
    """
    try:
        plugin = plugin or _caller_plugin(skip=2)
        surface = surface or _surface_of(plugin)
        reg = _registry()
        from app.plugins.capabilities.bootstrap import ensure_registered

        ensure_registered(surface)
        host = _resolve_host(plugin, surface)
        pool = reg.build_pool(surface=surface, host=host, plugin=plugin)
        if cap_id not in pool:
            logger.debug(f"[caps_for] plugin={plugin or '(builtin)'} cap={cap_id} 未授权或未注册，返回 None")
            return None
        return pool[cap_id]
    except Exception as e:
        logger.debug(f"[caps_for] 取能力 {cap_id} 失败: {e}")
        return None


def pool_for(plugin: str = "", surface: str = "", host: Any = None) -> Optional[Any]:
    """取整个能力池视图（需要多个能力 / 要判 ``in`` 时用这个）。

    host 显式传入时优先用它；否则按插件名解析（见 :func:`caps_for`）。
    """
    try:
        plugin = plugin or _caller_plugin(skip=2)
        surface = surface or _surface_of(plugin)
        from app.plugins.capabilities.bootstrap import ensure_registered

        reg = _registry()
        ensure_registered(surface)
        if host is None:
            host = _resolve_host(plugin, surface)
        return reg.build_pool(surface=surface, host=host, plugin=plugin)
    except Exception as e:
        logger.debug(f"[pool_for] 建池失败 plugin={plugin}: {e}")
        return None


def declared_for(plugin: str = "") -> Optional[FrozenSet[str]]:
    """该插件的已解析能力清单（None = 兼容期全量放行）。排查用。"""
    try:
        plugin = plugin or _caller_plugin(skip=2)
        return _registry().resolve_declared(plugin)
    except Exception:
        return None
