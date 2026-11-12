# -*- coding: utf-8 -*-
"""插件宿主补载能力：plugin.reload_components（UI surface）。

B 项（内置插件启停去双通道）的宿主侧封装：worker 线程只写启停配置，全组件
口径的补载/补卸载与 plugin_changed 广播收敛到本能力，由插件市场主线程在
worker 完成后调用。私有性封装在主程序侧——PluginHostService 的内部方法不
暴露给插件（架构红线：插件禁止直接 import 主程序内部包）。
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def reload_components(host: Any):
    """插件启停后的全组件补载/补卸载（含 plugin_changed 后置广播）。

    返回 ``(name: str, enabled: bool) -> dict``（各组件重载结果；插件名不存在
    或异常时返回 {}）。

    - enabled=True：``PluginHostService._reload_single_plugin(name, "__manifest__")``
      遍历插件全部已声明组件精准加载（与 enable_plugin 原联动加载同口径）
    - enabled=False：``_cleanup_removed_plugin_components`` 按「插件已删除」语义
      全组件清理（与 disable_plugin 原联动卸载同口径）
    - 完成后 emit_plugin_changed（hook + UI 广播一次到位，消费方不见中间态）
    """

    def _reload(name: str, enabled: bool) -> dict:
        try:
            from app.core.services.plugin_host_service import PluginHostService
            from app.plugins.kernel import KNOWN_COMPONENTS
            from app.plugins.managers.plugin_manager import PluginManager

            svc = PluginHostService.get_instance()
            if enabled:
                result = svc._reload_single_plugin(name, "__manifest__")
            else:
                pm = PluginManager.get_instance()
                plugin = pm.get_plugin(name)
                components = (
                    dict(plugin.components)
                    if plugin is not None and isinstance(getattr(plugin, "components", None), dict)
                    else {}
                )
                manifest = (
                    dict(plugin.manifest)
                    if plugin is not None and isinstance(getattr(plugin, "manifest", None), dict)
                    else None
                )
                result: dict = {k: (0 if k == "agents" else False) for k in KNOWN_COMPONENTS}
                svc._cleanup_removed_plugin_components(
                    name, components, result, tuple(KNOWN_COMPONENTS), manifest=manifest
                )
            # plugin_changed 后置：补载完成后才广播（hook + UI 一次到位），两分支统一
            svc.emit_plugin_changed(result, name, action="enabled" if enabled else "disabled")
            return result
        except Exception as e:  # noqa: BLE001 — 补载失败不阻断市场 UI 回调链
            logger.warning(f"[Capability:plugin.reload_components] 补载失败 ({name}, enabled={enabled}): {e}")
            return {}

    return _reload
