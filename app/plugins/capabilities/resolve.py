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

import pathlib
from typing import Any, FrozenSet, Optional

from loguru import logger

# 插件名 → 该插件当前绑定的窗口（浮动卡 ctx 建立时登记）。
# 多窗口同一插件各开一张卡时记最后一个：卡片的 host 本就是"它显示所在窗口"，
# 后台线程取的是最近一次交互过的窗口，与 window.current 的活跃窗口语义一致。
_plugin_hosts: dict[str, Any] = {}

# 调用方文件路径 → 解析结果缓存（文件名 / 插件名 / surface）。
# ★ 不能用 inspect.stack()：它会为每一帧做 findsource → getmodule → realpath，
# 每帧都是真实文件系统调用（实测深栈单次 40ms+，Qt 事件派发链里点一次卡片
# 触发 _caps() 时吃到 2s）。sys._getframe 只取帧对象，零 I/O。
_CALLER_CACHE: dict[str, tuple[str, str]] = {}
_CALLER_CACHE_MAX = 512


def _cache_caller(filename: str, plugin: str, surface: str) -> tuple[str, str]:
    """写入调用方解析缓存（超限整体清空，条目本身无状态，清空代价可忽略）。"""
    if len(_CALLER_CACHE) >= _CALLER_CACHE_MAX:
        _CALLER_CACHE.clear()
    _CALLER_CACHE[filename] = (plugin, surface)
    return plugin, surface


def _caller_info(skip: int) -> tuple[str, str]:
    """调用方 (插件名, surface)；解析不出返回 ("", "ui")。

    一次取帧、一次解析，同时供 _caller_plugin / caps_for 使用——两者原本
    各调一次 inspect.stack()，同一次 caps_for 就要走两遍完整栈扫描。
    """
    import sys

    try:
        filename = sys._getframe(skip).f_code.co_filename
    except ValueError, AttributeError:
        return "", "ui"
    hit = _CALLER_CACHE.get(filename)
    if hit is not None:
        return hit

    parts = pathlib.PurePath(filename).parts
    plugin = ""
    surface = "ui"
    if "plugins" in parts:
        idx = parts.index("plugins") + 1
        if idx < len(parts):
            plugin = parts[idx]
            # plugins/<name>/tools/ 或 hooks/ → tool surface。
            # hook 跑在引擎事件上下文，无窗口，host 语义与 tool 池（host=None）
            # 一致，tool-only 能力（mcp.call 等）由此在 hook 内可用。
            # 代价：ui-only 能力（ui.notify 等）在 hook 内拿不到，需显式传 surface="ui"。
            rest = parts[idx + 1 :]
            if "tools" in rest or "hooks" in rest:
                surface = "tool"
    return _cache_caller(filename, plugin, surface)


def _caller_plugin(skip: int = 1) -> str:
    """调用方的插件名（``plugins/<name>/...`` 的 ``<name>``）。

    推断不出返回空串（池记审计时显示为 ``(builtin)``）。
    ``skip`` 的基准是**本函数自身的帧**（0 = 本函数），与旧 inspect.stack 语义一致。
    """
    return _caller_info(max(0, skip) + 1)[0]


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


def active_main_widget() -> Any:
    """当前活跃对话窗口（多标签下 = 用户此刻所在标签页）。

    ★ 不能用 ``UIPluginRegistry._main_widget``：它由窗口 __init__ 的
    ``set_main_widget(self)`` 写入，语义是「最后构造的窗口」= 最新标签页，
    切标签页不会更新。历史 bug：全局卡（历史会话页等）在主线程回调里经
    ``caps_for`` 取 host 时拿到最新标签页，加载会话 / 切项目 / 新建会话
    全部作用到错误标签页。

    解析顺序：TabManagerWindow 当前选中 → _main_widget → 任一已登记窗口。
    """
    try:
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm is not None:
            win = tm.get_current_window()
            if win is not None and not getattr(win, "_is_destroyed", False):
                return win
    except Exception:
        pass
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        return getattr(reg, "_main_widget", None) or next(
            iter((getattr(reg, "_window_main_widgets", None) or {}).values()), None
        )
    except Exception:
        return None


def _resolve_host(plugin: str, surface: str) -> Any:
    """按插件名解析 host：绑定窗口 → 活跃窗口 → surface 默认 host。"""
    host = _plugin_hosts.get(plugin)
    if host is not None:
        return host
    return active_main_widget()


def _registry():
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    return CapabilityRegistry.get_instance()


def _caller_target(plugin: str, surface: str) -> tuple[str, str]:
    """补齐未显式传入的 (plugin, surface)：只取一次帧，不重复扫栈。

    取帧深度 3：0=_caller_info / 1=_caller_target / 2=caps_for|pool_for /
    3=真实调用方。**不能写 2**——那会拿到本模块自己的文件名，插件名恒为空，
    授权退化成「兼容期全量放行」（声明裁剪静默失效）。
    """
    if plugin and surface:
        return plugin, surface
    caller_plugin, caller_surface = _caller_info(3)
    return plugin or caller_plugin, surface or caller_surface


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
        plugin, surface = _caller_target(plugin, surface)
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
        plugin, surface = _caller_target(plugin, surface)
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
