# -*- coding: utf-8 -*-
"""市场数据源 — 远程拉取 + 本地缓存"""
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger


MARKETPLACE_URL = (
    "https://raw.githubusercontent.com/martin98-afk/drifox-plugins/"
    "main/marketplace.json"
)
CACHE_TTL = 3600  # 1 小时


class MarketplaceData:
    """市场数据获取 + 缓存"""

    def __init__(self):
        self._cache_file = Path.home() / '.drifox' / "cache" / "marketplace.json"
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._data: Optional[Dict[str, Any]] = None
        self._fetched_at: float = 0

    def fetch(self, force: bool = False) -> Dict[str, Any]:
        """获取市场数据（带缓存）

        Args:
            force: 强制刷新（忽略缓存）

        Returns:
            {"name": ..., "description": ..., "plugins": [...]}
        """
        if not force and self._data is not None and (time.time() - self._fetched_at) < CACHE_TTL:
            return self._data
        if not force and self._cache_file.exists():
            try:
                data = json.loads(self._cache_file.read_text(encoding="utf-8"))
                self._data = data
                self._fetched_at = self._cache_file.stat().st_mtime
                return data
            except Exception as e:
                logger.warning(f"[Marketplace] Cache read failed: {e}")
        # 拉取远程
        try:
            response = httpx.get(MARKETPLACE_URL, timeout=15)
            response.raise_for_status()
            data = response.json()
            self._cache_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._data = data
            self._fetched_at = time.time()
            return data
        except Exception as e:
            logger.error(f"[Marketplace] Fetch failed: {e}")
            # 降级：返回空数据
            return {"name": "drifox-official", "description": "", "plugins": []}

    def list_plugins(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出插件（可选按 category 过滤）"""
        data = self.fetch()
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


def get_marketplace() -> MarketplaceData:
    global _instance
    if _instance is None:
        _instance = MarketplaceData()
    return _instance
