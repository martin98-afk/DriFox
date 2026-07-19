# -*- coding: utf-8 -*-
"""市场源管理器 — 多市场源的增删改查、拉取合并

持久化到 .drifox/cache/marketplace_sources.json，不依赖 app 核心 Settings。
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger


# ── 环境检测 ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEV_DRIFOX = _PROJECT_ROOT / ".drifox"
_USER_DRIFOX = Path.home() / ".drifox"


def _drifox_dir() -> Path:
    """查找 .drifox 目录（开发环境优先，兜底用户目录）"""
    if _DEV_DRIFOX.exists():
        return _DEV_DRIFOX
    return _USER_DRIFOX


# ── 默认市场源 ─────────────────────────────────────────────

_DEFAULT_SOURCES = [
    {
        "name": "drifox-official",
        "source": {
            "source": "url",
            "url": "https://raw.githubusercontent.com/martin98-afk/drifox-plugins/main/marketplace.json",
        },
        "auto_update": True,
        "builtin": True,
    }
]


class MarketplaceSourceManager:
    """管理多个市场源：增删、持久化、拉取"""

    def __init__(self):
        drifox = _drifox_dir()
        self._cache_dir = drifox / "cache" / "marketplaces"
        self._sources_file = self._cache_dir / "sources.json"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # 确保默认源存在
        self._ensure_defaults()

    def _ensure_defaults(self):
        """确保默认市场源已写入持久化文件"""
        if self._sources_file.exists():
            return
        self._sources_file.write_text(
            json.dumps(_DEFAULT_SOURCES, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── 源管理 ──

    def get_sources(self) -> List[Dict[str, Any]]:
        """获取所有已添加的市场源"""
        if not self._sources_file.exists():
            self._ensure_defaults()
        try:
            return json.loads(self._sources_file.read_text(encoding="utf-8"))
        except Exception:
            return list(_DEFAULT_SOURCES)

    def _save_sources(self, sources: List[Dict[str, Any]]):
        """保存市场源列表到文件"""
        self._sources_file.write_text(
            json.dumps(sources, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add_source(self, name: str, source: Dict[str, Any], auto_update: bool = False) -> bool:
        """添加市场源

        Args:
            name: 市场名（如 "claude-community"）
            source: {"source": "github", "repo": "..."} 或 {"source": "url", "url": "..."}
            auto_update: 是否自动更新

        Returns:
            True 添加成功
        """
        sources = self.get_sources()
        # 同名覆盖
        for s in sources:
            if s["name"] == name:
                sources.remove(s)
                logger.info(f"[Marketplace] 覆盖已有市场源: {name}")
                break
        entry = {
            "name": name,
            "source": source,
            "auto_update": auto_update,
            "builtin": False,
        }
        sources.append(entry)
        self._save_sources(sources)
        logger.info(f"[Marketplace] 添加市场源: {name}")
        return True

    def remove_source(self, name: str) -> bool:
        """移除市场源"""
        sources = self.get_sources()
        new_sources = [s for s in sources if s["name"] != name]
        if len(new_sources) == len(sources):
            return False
        self._save_sources(new_sources)
        logger.info(f"[Marketplace] 移除市场源: {name}")
        return True

    # ── 拉取市场数据 ──

    def fetch_marketplace(self, source_def: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        """拉取单个市场的 marketplace.json 数据

        Args:
            source_def: {"name": "...", "source": {...}, ...}
            force: 强制忽略缓存

        Returns:
            {"name": "...", "description": "...", "plugins": [...]}
        """
        name = source_def["name"]
        src = source_def["source"]
        cache_file = self._cache_dir / f"{name}.json"

        # 缓存命中
        if not force and cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < 3600:  # 1 小时 TTL
                try:
                    return json.loads(cache_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

        market_data = None
        src_type = src.get("source", "url")

        try:
            if src_type == "url":
                url = src["url"]
                resp = httpx.get(url, timeout=15, follow_redirects=True)
                resp.raise_for_status()
                market_data = resp.json()
            elif src_type == "github":
                repo = src["repo"]
                ref = src.get("ref", "main")
                url = f"https://raw.githubusercontent.com/{repo}/{ref}/.claude-plugin/marketplace.json"
                resp = httpx.get(url, timeout=15, follow_redirects=True)
                resp.raise_for_status()
                market_data = resp.json()
            else:
                logger.warning(f"[Marketplace] 不支持的市场源类型: {src_type}")
                return {
                    "name": name, "description": "", "plugins": [],
                    "_error": f"Unsupported source: {src_type}",
                }
        except Exception as e:
            if name == "__tmp__":
                logger.debug(f"[Marketplace] 验证拉取市场失败 (预期内): {e}")
            else:
                logger.error(f"[Marketplace] 拉取市场 {name} 失败: {e}")
            if cache_file.exists():
                try:
                    return json.loads(cache_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return {"name": name, "description": "", "plugins": [], "_error": str(e)}

        # 确保 plugins 字段存在
        if "plugins" not in market_data:
            market_data["plugins"] = []

        # 为每个插件标记来源市场
        for plugin in market_data.get("plugins", []):
            plugin["_marketplace"] = name
            plugin["_marketplace_source"] = src

        # 缓存
        cache_file.write_text(
            json.dumps(market_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return market_data

    def fetch_all(self, force: bool = False) -> tuple:
        """拉取所有市场的插件列表（合并）

        Returns:
            (all_plugins: list, marketplaces: list, errors: list)
        """
        all_plugins: Dict[str, Dict[str, Any]] = {}
        marketplaces: List[Dict[str, Any]] = []
        errors: List[str] = []

        for src_def in self.get_sources():
            data = self.fetch_marketplace(src_def, force=force)
            if data.get("_error"):
                errors.append(f"{src_def['name']}: {data['_error']}")
            marketplaces.append(data)
            for plugin in data.get("plugins", []):
                name = plugin.get("name", "")
                if name and name not in all_plugins:
                    all_plugins[name] = plugin

        return list(all_plugins.values()), marketplaces, errors

    def refresh_source(self, name: str) -> bool:
        """强制刷新指定市场源"""
        sources = self.get_sources()
        for src_def in sources:
            if src_def["name"] == name:
                self.fetch_marketplace(src_def, force=True)
                return True
        return False


# ── 单例 ──

_instance: Optional[MarketplaceSourceManager] = None


def get_marketplace_manager() -> MarketplaceSourceManager:
    global _instance
    if _instance is None:
        _instance = MarketplaceSourceManager()
    return _instance
