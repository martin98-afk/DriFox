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


# ── 环境检测 ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEV_DRIFOX = _PROJECT_ROOT / ".drifox"
_USER_DRIFOX = Path.home() / ".drifox"


def _drifox_dir() -> Path:
    """查找 .drifox 目录（开发环境优先，兜底用户目录）"""
    if _DEV_DRIFOX.exists():
        return _DEV_DRIFOX
    return _USER_DRIFOX


class MarketplaceData:
    """市场数据获取 + 缓存"""

    def __init__(self):
        self._cache_file = _drifox_dir() / "cache" / "marketplace.json"
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

    def list_plugins(self, category: Optional[str] = None, force: bool = False) -> List[Dict[str, Any]]:
        """列出插件（可选按 category 过滤）

        Args:
            category: 分类过滤
            force: 是否强制拉取远程（跳过缓存）
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

    # 补齐到相同长度
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
