# -*- coding: utf-8 -*-
"""
服务商图标解析 — 插件自包含图标 + 主题感知 + 回退 qrc。

「万物为插件」：服务商图标由 providers 插件自带
（<插件>/providers/icons/ 深色 + icons_light/ 浅色），
主程序 qrc 仅作回退兜底。与 tools 插件的图标机制对称。
"""

from collections import OrderedDict
from pathlib import Path

from PyQt5.QtGui import QIcon

# LRU 缓存（max 256），超限驱逐最久未访问条目，避免插件图标字典无限膨胀。
_ICON_CACHE: OrderedDict = OrderedDict()
_ICON_CACHE_MAX = 256


def _is_light_theme() -> bool:
    """当前主题是否浅色（延迟 import 避免循环依赖）"""
    try:
        from app.utils.theme_manager import theme_manager

        return theme_manager.is_light_theme()
    except Exception:
        return False


def _provider_icon_path(provider_name: str) -> str:
    """按当前主题解析服务商插件图标文件路径（无则空串）。

    优先级：浅色主题 icons_light/{icon}.svg → icons_light/{icon}.png
           → 深色 icons/{icon}.svg → icons/{icon}.png → 空
    """
    try:
        from app.plugins.registries.provider_registry import ProviderRegistry

        p = ProviderRegistry.get_instance().get(provider_name)
        if p is None or not p.icon:
            return ""
        base_dirs = [p.icon_dir_light, p.icon_dir] if _is_light_theme() else [p.icon_dir, p.icon_dir_light]
        for d in base_dirs:
            if not d:
                continue
            for ext in (".svg", ".png"):
                candidate = Path(d) / f"{p.icon}{ext}"
                if candidate.exists():
                    return str(candidate)
    except Exception:
        pass
    return ""


def get_provider_icon(provider_name: str) -> QIcon:
    """获取服务商图标（插件目录优先，qrc 回退）。

    主题感知：浅色主题优先 icons_light/ 目录，深色优先 icons/。
    插件图标缺失时回退主程序 qrc 的 PROVIDER_ICONS 图标。
    与 get_icon 的 _ThemeIconEngine 不同，本函数直接加载文件路径
    （QIcon 原生支持 svg/png），无 QIconEngine 缓存复用问题。
    """
    try:
        from app.plugins.registries.provider_registry import ProviderRegistry

        p = ProviderRegistry.get_instance().get(provider_name)
        if p is None:
            return QIcon()
        cache_key = f"{provider_name}:{p.icon}:{p.icon_dir}:{p.icon_dir_light}:{_is_light_theme()}"
        if cache_key in _ICON_CACHE:
            _ICON_CACHE.move_to_end(cache_key)
            return _ICON_CACHE[cache_key]

        path = _provider_icon_path(provider_name)
        if path:
            icon = QIcon(path)
            if not icon.isNull():
                _ICON_CACHE[cache_key] = icon
                if len(_ICON_CACHE) > _ICON_CACHE_MAX:
                    _ICON_CACHE.popitem(last=False)
                return icon

        # 回退：主程序 qrc（按 icon key 取，主题感知由 _ThemeIconEngine 处理）
        from app.utils.utils import get_icon

        icon = get_icon(p.icon)
        _ICON_CACHE[cache_key] = icon
        if len(_ICON_CACHE) > _ICON_CACHE_MAX:
            _ICON_CACHE.popitem(last=False)
        return icon
    except Exception:
        from app.utils.utils import get_icon

        return get_icon("大模型")


def invalidate_provider_icon_cache():
    """清除服务商图标 LRU 缓存（主题切换/会话清理时调用）"""
    _ICON_CACHE.clear()