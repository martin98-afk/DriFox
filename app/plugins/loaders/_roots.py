# -*- coding: utf-8 -*-
"""loaders 共享的插件根定位与加载门禁工具。

plugin_tool_loader / provider_loader / runtime_component_loader 三处原本
各自维护逐字同构的 _ROOT_KIND_* 常量与 _plugin_roots/_root_kind，收敛于此。
注意 __file__ 仍位于 app/plugins/loaders/ 下，四层 parent 到项目根的推导不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

# 插件根等级：system < user。低等级根注册的同名工具可被高等级根覆盖
_ROOT_KIND_SYSTEM = "system"  # 项目工作树 plugins/（含 plugins/system/）
_ROOT_KIND_USER = "user"  # 用户数据目录 <app_data>/plugins/
_ROOT_KIND_PRIORITY: Dict[str, int] = {_ROOT_KIND_SYSTEM: 0, _ROOT_KIND_USER: 1}


def _plugin_roots() -> List[Path]:
    """插件扫描根：项目 plugins/（含 system）+ 用户插件目录"""
    roots = [Path(__file__).resolve().parent.parent.parent.parent / "plugins"]
    try:
        from app.utils.utils import get_app_data_dir

        roots.append(get_app_data_dir() / "plugins")
    except Exception:
        pass
    return roots


def _root_kind(root: Optional[Path]) -> str:
    """识别 root 等级：项目工作树 → system；app_data/plugins → user

    测试场景传入自定义 root（既非工作树也非 app_data），按 system 兜底。
    """
    if root is None:
        return _ROOT_KIND_SYSTEM
    try:
        from app.utils.utils import get_app_data_dir

        user_root = get_app_data_dir() / "plugins"
        if root == user_root:
            return _ROOT_KIND_USER
    except Exception:
        pass
    return _ROOT_KIND_SYSTEM


def _is_plugin_load_blocked(plugin_name: str, *, log_tag: str) -> bool:
    """P1/P2：检查插件是否被版本/平台门禁拦截（load_blocked）。

    仅在 PluginManager 已初始化且能查到插件时检查；否则视为不拦截（放行）。

    log_tag：各 loader 自带的日志前缀（如 [ProviderLoader]），失败放行日志
    沿用调用方前缀，便于定位来源。
    """
    try:
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        if not pm.is_initialized():
            return False
        plugin = pm.get_plugin(plugin_name)
        if plugin is None:
            return False
        return bool(getattr(plugin, "load_blocked", False))
    except Exception as e:
        logger.warning(f"{log_tag} 门禁检查失败，默认放行 {plugin_name}: {e}")
        return False
