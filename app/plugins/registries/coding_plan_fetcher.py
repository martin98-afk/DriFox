# -*- coding: utf-8 -*-
"""
套餐用量获取器注册表（薄壳）— fetcher 已迁移至 providers 插件。

历史版本在此直接实现 OpenCode / 火山 / MiniMax / 智谱 / OpenAI 的
套餐用量抓取逻辑并维护本地注册表。服务商插件化后：
- 各 fetcher 逻辑移入 plugins/system/providers/<provider>.py 插件文件
- 统一注册表为 app.plugins.registries.provider_registry.ProviderRegistry
  （ProviderDef.coding_plan_fetcher 字段）

本文件保留薄壳 API（fetch / fetch_async / register / get），全部委托
ProviderRegistry，兼容存量调用方与性能脚本（perf_regression.py 的
fetch_async monkeypatch 目标）。
"""
from typing import Any, Callable, Dict, Optional

from app.plugins.registries.provider_registry import ProviderRegistry

# 类型兼容（返回 fetcher 或 None）
CodingPlanFetcher = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]


def register(provider_name: str, fetcher: CodingPlanFetcher):
    """[已迁移] 注册一个服务商的套餐用量获取器（新实现请用插件 ProviderDef）"""
    p = ProviderRegistry.get_instance().get(provider_name)
    if p is not None:
        p.coding_plan_fetcher = fetcher


def get(provider_name: str) -> Optional[CodingPlanFetcher]:
    """获取指定服务商的获取器（委托注册表）"""
    registry = ProviderRegistry.get_instance()
    return registry._resolve_coding_plan_fetcher(provider_name)


def fetch(provider_name: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按服务商名称查询套餐用量（同步，可能阻塞网络）"""
    return ProviderRegistry.get_instance().coding_plan_fetch(provider_name, config)


def fetch_async(
    provider_name: str,
    config: Dict[str, Any],
    callback: Callable[[Optional[Dict[str, Any]]], None],
):
    """异步查询套餐用量，结果通过 callback 返回（后台线程执行）"""
    import threading

    def _run():
        try:
            result = fetch(provider_name, config)
        except Exception:
            result = None
        callback(result)

    threading.Thread(target=_run, daemon=True, name="coding-plan-fetch").start()