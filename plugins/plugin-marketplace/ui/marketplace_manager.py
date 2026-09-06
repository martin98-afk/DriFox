# -*- coding: utf-8 -*-
"""市场源管理器 — 多市场源的增删改查、拉取合并

市场源配置持久化到 .drifox/plugins/user-custom/marketplaces/sources.json
（随 user-custom 插件一起被云端备份/同步），拉取缓存仍在 cache 目录。
不依赖 app 核心 Settings。
"""

import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from .proxy import get_proxy_config


# ── 环境检测 ──────────────────────────────────────────────


def _drifox_dir() -> Path:
    """获取应用数据目录（与 app.utils.utils.get_app_data_dir 保持一致）

    开发环境: 仓库根/.drifox（解析为绝对路径，避免依赖可变 cwd）
    PyInstaller打包: ~/.drifox（用户 home 目录，可写）
    macOS .app: ~/Library/Application Support/Drifox/.drifox

    注意：相对路径 Path(".drifox") 会在每次文件操作时按当前 cwd 解析；
    Windows 原生 QFileDialog 会静默改变进程 cwd，导致相对路径被解析到
    错误目录而 FileNotFoundError。开发环境改为基于 __file__ 定位仓库根，
    返回绝对路径，彻底脱离 cwd 依赖。
    """
    if not hasattr(sys, "_MEIPASS") and not getattr(sys, "frozen", False):
        here = Path(__file__).resolve()
        root = here.parent
        for _ in range(8):
            if (root / ".git").exists():
                return root / ".drifox"
        return here.parents[3] / ".drifox"
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
    },
    {
        "name": "drifox-system",
        "source": {
            "source": "url",
            "url": "https://raw.githubusercontent.com/martin98-afk/DriFox/master/marketplace.json",
        },
        "auto_update": True,
        "builtin": True,
    },
]


# ── C1：市场源白名单 ──────────────────────────────

# 允许的 git 托管 host（url 类型源）；Settings.marketplace_allowed_git_hosts 可追加
_MARKETPLACE_ALLOWED_GIT_HOSTS = {"github.com", "gitlab.com", "gitee.com", "bitbucket.org", "gitcode.com"}
# github 类型源的 repo 形态：owner/name
_GITHUB_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def _marketplace_allowed_hosts() -> set:
    """内置白名单 + Settings.marketplace_allowed_git_hosts 扩展（内网 git 源显式加白）。

    本模块设计上不依赖 app 核心 Settings，故此处容错读取：不可用时仅用内置表。
    """
    hosts = set(_MARKETPLACE_ALLOWED_GIT_HOSTS)
    try:
        from app.utils.config import Settings

        extra = Settings.get_instance().marketplace_allowed_git_hosts.value or []
        hosts |= {str(h).strip().lower() for h in extra if str(h).strip()}
    except Exception:
        pass
    return hosts


def _validate_git_url_field(raw: Any, *, allow_shorthand: bool) -> Tuple[bool, str]:
    """校验 git url 字段：https 直链走 host 白名单；简写仅按需放行。

    allow_shorthand=True 时兼容 owner/repo 简写（隐含 github.com，与
    installer._resolve_git_subdir_url 的补全行为对齐，仅 git-subdir 使用）。
    """
    from urllib.parse import urlparse

    raw = str(raw or "").strip()
    if not raw:
        return False, "url 缺失"
    if allow_shorthand and "/" in raw and not raw.startswith(("http://", "https://", "git@")):
        if _GITHUB_REPO_RE.fullmatch(raw):
            return True, ""
        return False, f"url 简写不合法: {raw!r}（须为 owner/repo 形态或 https 直链）"
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        return False, f"url scheme 须为 https（当前: {parsed.scheme or '缺失'}）"
    host = (parsed.hostname or "").lower()
    if host not in _marketplace_allowed_hosts():
        return False, f"非白名单 git host: {host or '缺失'}"
    return True, ""


def validate_marketplace_source(source: Any) -> Tuple[bool, str]:
    """C1：市场源白名单校验（新增/修改与下载前二次校验共用）。

    - github → repo 必须匹配 ^[\w.-]+/[\w.-]+$
    - url → scheme 必须 https 且 hostname 在允许表（内置五家 + Settings 扩展）
    - git-subdir → url 同 url 规则（另兼容 owner/repo 简写）；path 必须为
      仓库内相对子目录（禁绝对路径与 .. 穿越）。注意：安装器本就支持
      git-subdir（DriFox 旧格式，drifox-plugins 市场全量在用），
      校验器必须与其能力面对齐，否则合法安装被误拦。
    - 其余类型与 file:///ssh:// 等一律拒绝

    Returns:
        (ok, reason) — ok=False 时 reason 为拒收原因。
    """
    try:
        if not isinstance(source, dict):
            return False, f"不支持的 source 类型: {type(source).__name__}"
        src_type = str(source.get("source") or source.get("type") or "").strip().lower()
        if src_type == "github":
            repo = str(source.get("repo") or "").strip()
            segments = repo.split("/")
            dot_segment = any(seg in (".", "..") for seg in segments)
            if (
                len(segments) == 2
                and not dot_segment
                and all(segments)
                and _GITHUB_REPO_RE.fullmatch(repo)
            ):
                return True, ""
            return False, f"github repo 不合法: {repo!r}（须为 owner/name 形态，禁路径穿越）"
        if src_type in ("url", "git-subdir"):
            ok, reason = _validate_git_url_field(source.get("url"), allow_shorthand=(src_type == "git-subdir"))
            if not ok:
                return False, reason
            if src_type == "git-subdir":
                subpath = str(source.get("path") or "").strip()
                norm = subpath.replace("\\", "/")
                parts = [seg for seg in norm.split("/") if seg]
                if (
                    not subpath
                    or norm.startswith("/")
                    or (len(norm) >= 2 and norm[1] == ":")
                    or any(seg in (".", "..") for seg in parts)
                ):
                    return False, f"git-subdir path 不合法: {subpath!r}（须为仓库内相对子目录，禁路径穿越）"
            return True, ""
        return False, f"不支持的市场源类型: {src_type or '缺失'}"
    except Exception as e:
        return False, f"校验异常: {e}"


class MarketplaceSourceManager:
    """管理多个市场源：增删、持久化、拉取"""

    # status.json 并发写锁（类级：兼容测试用 __new__ 手赋属性的构造，
    # 多市场并行拉取时 _record_status/_save_status 可能被多个 worker 线程
    # 同时调用写同一文件，锁保证不互相覆盖/损坏）
    _status_lock = threading.Lock()

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
        """确保默认市场源已写入持久化文件

        - 文件不存在：写入全部默认源
        - 文件已存在：按 name 将缺失的 builtin 默认源追加（去重合并）。
          存量用户升级后自动补齐新增的 builtin 源（如 drifox-system），
          保留用户自定义源的顺序与字段，不覆盖任何已有条目。
        """
        if not self._sources_file.exists():
            self._sources_file.write_text(
                json.dumps(_DEFAULT_SOURCES, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return
        try:
            # utf-8-sig 兼容带 BOM 的文件（用户用记事本编辑后可能引入 BOM）
            existing = json.loads(self._sources_file.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[Marketplace] 读取市场源配置失败，跳过默认源合并: {e}")
            return
        if not isinstance(existing, list):
            logger.warning("[Marketplace] 市场源配置格式异常，跳过默认源合并")
            return
        existing_names = {s.get("name") for s in existing if isinstance(s, dict)}
        missing = [d for d in _DEFAULT_SOURCES if d.get("name") not in existing_names]
        if not missing:
            return
        try:
            self._sources_file.write_text(
                json.dumps(existing + missing, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"[Marketplace] 已追加缺失的默认市场源: {[m.get('name') for m in missing]}")
        except OSError as e:
            logger.warning(f"[Marketplace] 写回市场源配置失败: {e}")

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
        with self._status_lock:
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
        with self._status_lock:
            return self._fetch_status.get(name)

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """获取全部市场源拉取状态"""
        with self._status_lock:
            return dict(self._fetch_status)

    def clear_status(self, name: str):
        """清除某市场源的拉取状态（源被移除时调用）"""
        with self._status_lock:
            self._fetch_status.pop(name, None)
            self._save_status()
        """清除某市场源的拉取状态（源被移除时调用）"""
        self._fetch_status.pop(name, None)
        self._save_status()

    # ── 源管理 ──

    def get_sources(self) -> List[Dict[str, Any]]:
        """获取所有已添加的市场源"""
        if not self._sources_file.exists():
            self._ensure_defaults()
        try:
            sources = json.loads(self._sources_file.read_text(encoding="utf-8-sig"))
        except Exception:
            return list(_DEFAULT_SOURCES)
        # C1 存量容忍：既有非法条目仅告警不阻断/不删除（强校验只管新增/修改）
        for entry in sources or []:
            if isinstance(entry, dict) and entry.get("source") is not None:
                ok, reason = validate_marketplace_source(entry.get("source"))
                if not ok:
                    logger.warning(
                        f"[MarketplaceGuard] 存量市场源未过白名单（容忍放行，仅告警）: "
                        f"{entry.get('name')} {reason}"
                    )
        return sources or []

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
        # C1：新增/修改走强校验（存量条目不受影响，见 get_sources 容忍逻辑）
        ok, reason = validate_marketplace_source(source)
        if not ok:
            logger.warning(f"[MarketplaceGuard] 拒绝添加市场源: {name} {reason}")
            return False
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
        同时给市场数据顶层补 _marketplace = 源名：marketplace.json 自带 name
        可能与源名不一致（如 claude-plugins-community 数据 name 是
        "claude-community"），而插件 _marketplace 统一用源名；合并/状态
        徽标按源名匹配，避免「旧数据不移除导致列表翻倍」。
        """
        market_data.setdefault("_marketplace", name)
        for plugin in market_data.get("plugins", []) or []:
            if not isinstance(plugin, dict):
                continue
            plugin.setdefault("_marketplace", name)
            plugin.setdefault("_marketplace_source", src)

    def _http_get(self, url: str, **kwargs) -> "httpx.Response":
        """应用代理发起 GET；代理失败回退直连

        - 未启用代理：直接请求原 URL（零开销）
        - 启用代理：先请求改写 URL（或带 proxy 参数），失败后
          换原 URL 直连重试一次，日志记录回退原因。
        """
        proxy = get_proxy_config()
        proxied_url = proxy.rewrite_url(url)
        try:
            resp = httpx.get(proxied_url, **kwargs, **proxy.httpx_kwargs())
            resp.raise_for_status()
            return resp
        except Exception:
            if proxied_url != url:
                logger.warning(f"[Marketplace] proxy failed, fallback to direct: {url}")
                resp = httpx.get(url, **kwargs)
                resp.raise_for_status()
                return resp
            raise

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
                resp = self._http_get(url, timeout=15, follow_redirects=True)
                market_data = resp.json()
            elif src_type == "github":
                repo = src["repo"]
                ref = src.get("ref", "main")
                url = f"https://raw.githubusercontent.com/{repo}/{ref}/.claude-plugin/marketplace.json"
                resp = self._http_get(url, timeout=15, follow_redirects=True)
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

        # 缓存：写失败仅告警，不阻断已拉取的数据（目录可能因 cwd 变化而缺失）
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(market_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"[Marketplace] 市场缓存写入失败（跳过缓存）: {e}")

        self._record_status(
            name,
            ok=True,
            from_cache=False,
            plugins=len(market_data.get("plugins", []) or []),
        )

        return market_data

    def fetch_all(self, force: bool = False) -> tuple:
        """拉取所有市场的插件列表（合并，多市场并行拉取）

        各市场源互不依赖（不同 URL/缓存文件/状态），用线程池并发拉取：
        N 个源最坏等待时间从 15×N 秒降到 ~15 秒。
        合并逻辑保持与源顺序一致（executor.map 保序），同名插件 downloads 求和。

        Returns:
            (all_plugins: list, marketplaces: list, errors: list)
        """
        from concurrent.futures import ThreadPoolExecutor

        sources = self.get_sources()
        all_plugins: Dict[str, Dict[str, Any]] = {}
        marketplaces: List[Dict[str, Any]] = []
        errors: List[str] = []

        if sources:
            # 并发上限 4：过多线程同时建连意义不大，反增代理/源站压力
            max_workers = min(4, len(sources))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                results = list(pool.map(lambda s: self.fetch_marketplace(s, force=force), sources))
        else:
            results = []

        for src_def, data in zip(sources, results):
            if data.get("_error"):
                errors.append(f"{src_def['name']}: {data['_error']}")
            marketplaces.append(data)
            for plugin in data.get("plugins", []):
                name = plugin.get("name", "")
                if not name:
                    continue
                try:
                    dl = int(plugin.get("downloads", 0) or 0)
                except (TypeError, ValueError):
                    dl = 0
                if name in all_plugins:
                    # 同名插件：downloads 求和（缺失/异常值按 0 兜底）
                    existing = all_plugins[name]
                    try:
                        cur = int(existing.get("downloads", 0) or 0)
                    except (TypeError, ValueError):
                        cur = 0
                    existing["downloads"] = cur + dl
                else:
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
