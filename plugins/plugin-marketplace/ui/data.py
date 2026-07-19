# -*- coding: utf-8 -*-
"""市场数据源 — 多市场拉取 + 本地缓存"""
from typing import Any, Dict, List, Optional

from .marketplace_manager import get_marketplace_manager


class MarketplaceData:
    """市场数据获取（适配多市场）"""

    def __init__(self):
        self._mgr = get_marketplace_manager()

    def fetch(self, force: bool = False) -> Dict[str, Any]:
        """获取合并后的市场数据

        Args:
            force: 强制刷新所有市场

        Returns:
            {
                "name": "combined",
                "description": "All marketplaces",
                "plugins": [...],
                "marketplaces": [...]   # 每个市场的完整数据
            }
        """
        all_plugins, marketplaces, errors = self._mgr.fetch_all(force=force)

        return {
            "name": "combined",
            "description": "All marketplaces",
            "plugins": all_plugins,
            "marketplaces": [
                {"name": m.get("name"), "description": m.get("description", "")}
                for m in marketplaces
            ],
            "_errors": errors,
        }

    def list_plugins(self, category: Optional[str] = None, force: bool = False) -> List[Dict[str, Any]]:
        """列出插件（可选按 category 过滤，保持向后兼容）

        Args:
            category: 分类过滤
            force: 是否强制拉取远程
        """
        data = self.fetch(force=force)
        plugins = data.get("plugins", [])
        if category:
            plugins = [p for p in plugins if category in (p.get("categories") or [])]
        return plugins

    def get_plugin(self, name: str) -> Optional[Dict[str, Any]]:
        """获取单个插件详情"""
        for p in self.list_plugins():
            if p.get("name") == name:
                return p
        return None


# 单例
_instance: Optional[MarketplaceData] = None


def compare_versions(v1: str, v2: str) -> int:
    """比较两个语义化版本号

    Args:
        v1: 版本号 A（如 "1.2.3"）
        v2: 版本号 B

    Returns:
        -1: v1 < v2
         0: v1 == v2
         1: v1 > v2
    """
    def parse(v: str):
        parts = v.strip().lstrip("v").split(".")
        return [int(p) if p.isdigit() else 0 for p in parts]

    parts1 = parse(v1)
    parts2 = parse(v2)

    max_len = max(len(parts1), len(parts2))
    parts1.extend([0] * (max_len - len(parts1)))
    parts2.extend([0] * (max_len - len(parts2)))

    for a, b in zip(parts1, parts2):
        if a < b:
            return -1
        elif a > b:
            return 1
    return 0


def get_marketplace() -> MarketplaceData:
    global _instance
    if _instance is None:
        _instance = MarketplaceData()
    return _instance
