# -*- coding: utf-8 -*-
"""市场源管理器 — 多市场源的增删改查、拉取合并

市场源配置持久化到 .drifox/plugins/user-custom/marketplaces/sources.json
（随 user-custom 插件一起被云端备份/同步），拉取缓存仍在 cache 目录。
不依赖 app 核心 Settings。
"""

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger


# ── 环境检测 ──────────────────────────────────────────────


def _drifox_dir() -> Path:
    """获取应用数据目录（与 app.utils.utils.get_app_data_dir 保持一致）

    开发环境: 当前目录/.drifox
    PyInstaller打包: ~/.drifox（用户 home 目录，可写）
    macOS .app: ~/Library/Application Support/Drifox/.drifox
    """
    if not hasattr(sys, "_MEIPASS") and not getattr(sys, "frozen", False):
        return Path(".drifox")
    if sys.platform == "darwin":
        try:
            from AppKit import NSApplicationSupportDirectory, NSFileManager, NSUserDomainMask

            paths = NSFileManager.defaultManager().URLsForDirectory_inDomains_(
                NSApplicationSupportDirectory, NSUserDomainMask
            )
            if paths:
                app_support_path = paths[0].fileSystemRepresentation().decode("utf-8")
                app_support = Path(app_support_path) / "Drifox"
                app_support.mkdir(parents=True, exist_ok=True)
                return app_support / ".drifox"
        except Exception:
            pass
    return Path.home() / ".drifox"


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
        # 市场源配置存入 user-custom 插件（可随 user-custom 一起云备份/同步）
        self._user_custom_dir = drifox / "plugins" / "user-custom"
        self._sources_dir = self._user_custom_dir / "marketplaces"
        self._sources_file = self._sources_dir / "sources.json"
        self._sources_dir.mkdir(parents=True, exist_ok=True)
        # 拉取的市场数据缓存仍放 cache 目录
        self._cache_dir = drifox / "cache" / "marketplaces"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # 各市场源最近一次拉取状态（持久化，跨会话可见）
        self._status_file = self._cache_dir / "status.json"
        self._fetch_status: Dict[str, Dict[str, Any]] = self._load_status()

        # 迁移旧版 cache 中的市场源配置（仅首次迁移）
        self._migrate_legacy_sources()
        # 确保默认源存在
        self._ensure_defaults()

    def _migrate_legacy_sources(self):
        """将旧版 cache/marketplaces/sources.json 迁移到 user-custom

        仅当新位置不存在且旧位置有配置时执行一次，避免用户已添加的市场源丢失。
        迁移成功后旧文件改名 .bak 备份：既保留回退数据，又避免用户删除
        新配置文件后被旧文件反复迁移（重置失效）。
        """
        if self._sources_file.exists():
            return
        legacy = self._cache_dir / "sources.json"
        if not legacy.exists():
            return
        try:
            self._sources_file.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
            legacy.rename(self._cache_dir / "sources.json.bak")
            logger.info("[Marketplace] 市场源配置已迁移: cache/marketplaces → user-custom/marketplaces")
        except Exception as e:
            logger.warning(f"[Marketplace] 市场源配置迁移失败: {e}")

    def _ensure_defaults(self):
        """确保默认市场源已写入持久化文件"""
        if self._sources_file.exists():
            return
        self._sources_file.write_text(
            json.dumps(_DEFAULT_SOURCES, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── 拉取状态 ──

    def _load_status(self) -> Dict[str, Dict[str, Any]]:
        """加载持久化的拉取状态（损坏/缺失 → 空）"""
        try:
            return json.loads(self._status_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_status(self):
        """持久化拉取状态（失败仅告警，不影响主流程）"""
        try:
            self._status_file.write_text(
                json.dumps(self._fetch_status, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[Marketplace] 拉取状态保存失败: {e}")

    def _record_status(
        self,
        name: str,
        *,
        ok: bool,
        error: str = "",
        from_cache: bool = False,
        plugins: int = 0,
    ):
        """记录某市场源最近一次拉取结果

        ok=True: 拉到数据（网络成功或缓存命中）
        ok=False: 拉取失败（含失败后回退缓存的情况，error 保留原因）
        """
        self._fetch_status[name] = {
            "ok": ok,
            "error": error,
            "from_cache": from_cache,
            "plugins": plugins,
            "time": time.time(),
        }
        self._save_status()

    def get_source_status(self, name: str) -> Optional[Dict[str, Any]]:
        """获取某市场源最近一次拉取状态，无记录返回 None"""
        return self._fetch_status.get(name)

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """获取全部市场源拉取状态"""
        return dict(self._fetch_status)

    def clear_status(self, name: str):
        """清除某市场源的拉取状态（源被移除时调用）"""
        self._fetch_status.pop(name, None)
        self._save_status()

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
        self.clear_status(name)
        logger.info(f"[Marketplace] 移除市场源: {name}")
        return True

    # ── 拉取市场数据 ──

    @staticmethod
    def _tag_cached_plugins(market_data: Dict[str, Any], name: str, src: Dict[str, Any]):
        """为插件补来源标记（旧缓存可能缺 _marketplace/_marketplace_source 字段）

        setdefault 不覆盖已存在字段，幂等。
        """
        for plugin in market_data.get("plugins", []) or []:
            if not isinstance(plugin, dict):
                continue
            plugin.setdefault("_marketplace", name)
            plugin.setdefault("_marketplace_source", src)

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
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    self._tag_cached_plugins(data, name, src)
                    self._record_status(
                        name,
                        ok=True,
                        from_cache=True,
                        plugins=len(data.get("plugins", []) or []),
                    )
                    return data
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
                self._record_status(name, ok=False, error=f"Unsupported source: {src_type}")
                return {
                    "name": name,
                    "description": "",
                    "plugins": [],
                    "_error": f"Unsupported source: {src_type}",
                }
        except Exception as e:
            if name == "__tmp__":
                logger.debug(f"[Marketplace] 验证拉取市场失败 (预期内): {e}")
            else:
                logger.error(f"[Marketplace] 拉取市场 {name} 失败: {e}")
            if cache_file.exists():
                try:
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    self._tag_cached_plugins(data, name, src)
                    # 失败回退缓存：状态记失败（error 保留原因），数据仍可用
                    self._record_status(
                        name,
                        ok=False,
                        error=str(e),
                        from_cache=True,
                        plugins=len(data.get("plugins", []) or []),
                    )
                    return data
                except Exception:
                    pass
            self._record_status(name, ok=False, error=str(e))
            return {"name": name, "description": "", "plugins": [], "_error": str(e)}

        # 确保 plugins 字段存在
        if "plugins" not in market_data:
            market_data["plugins"] = []

        # 为每个插件标记来源市场
        self._tag_cached_plugins(market_data, name, src)

        # 缓存
        cache_file.write_text(
            json.dumps(market_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self._record_status(
            name,
            ok=True,
            from_cache=False,
            plugins=len(market_data.get("plugins", []) or []),
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
