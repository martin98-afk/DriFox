# -*- coding: utf-8 -*-
"""插件安装器 — 通过 git 拉取市场插件

设计要点：
- 临时下载到 cache 目录，避免触发 watchfiles 插件热更新
- 下载完成后再原子式 move 到 plugins 目录
- 支持版本检测与更新
- 支持多种 source 类型（兼容 Claude Code marketplace 格式）
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from .data import compare_versions


# ── 下载量统计上报 ──────────────────────────────────────────
# 安装/更新成功后向计数服务上报 +1（尽力而为，失败不影响安装结果）。
# 计数服务与 key 命名必须与市场端 tools/downloads_stats.py 保持一致：
#   COUNT_API_BASE + /hit/drifox-plugins-{插件名}
# 服务说明：经典 countapi.xyz 已不稳定，使用其开源替代 CountAPI
# （countapi.mileshilliard.com，免注册）。如需换服务，同步改市场端常量。

_COUNT_API_BASE = "https://countapi.mileshilliard.com/api/v1"
_COUNT_KEY_PREFIX = "drifox-plugins-"
_COUNT_UA = "DriFox/0.5 (+https://github.com/martin98-afk/drifox-plugins)"


def report_plugin_install(plugin_name: str):
    """异步上报插件安装/更新计数（后台线程，绝不阻塞/影响安装流程）"""

    def _do():
        key = f"{_COUNT_KEY_PREFIX}{plugin_name}"
        req = urllib.request.Request(
            f"{_COUNT_API_BASE}/hit/{key}",
            headers={"User-Agent": _COUNT_UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            logger.debug(f"[Installer] 上报下载量成功: {plugin_name}")
        except Exception as e:
            logger.warning(f"[Installer] 上报下载量失败（不影响安装）: {plugin_name}: {e}")

    threading.Thread(target=_do, daemon=True, name=f"dl-report-{plugin_name}").start()


# ── 删除辅助 ──────────────────────────────────────────────


def _rmtree_readonly(path: Path) -> bool:
    """强制删除目录树（含只读文件）

    git clone 生成的 ``.git/objects/pack/*.idx|pack|rev`` 默认带只读属性，
    ``shutil.rmtree`` 不会清除该属性，在 Windows 上调用 ``os.unlink`` 会
    抛出 ``WinError 5 拒绝访问``。本方法在 ``onexc`` 回调里先 chmod 清除
    只读属性，再重试删除。

    Args:
        path: 要删除的目录

    Returns:
        True 删除成功；False 失败（保留原始异常日志）
    """

    def _onerror(func, failed_path, excinfo):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except OSError:
            pass

    try:
        # Python 3.12+ 新 API；项目要求 3.14+
        shutil.rmtree(path, onexc=_onerror)
        return True
    except Exception as e:
        logger.error(f"[Installer] rmtree {path} failed: {e}")
        return False


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


def _resolve_github_url(repo: str) -> str:
    """将 owner/repo 格式解析为 GitHub HTTPS URL"""
    return f"https://github.com/{repo}.git"


def _resolve_git_subdir_url(url_or_repo: str) -> str:
    """解析 git-subdir 的 url 字段（支持 owner/repo 简写）"""
    if "/" in url_or_repo and not url_or_repo.startswith(("http://", "https://", "git@")):
        return f"https://github.com/{url_or_repo}.git"
    return url_or_repo


class PluginInstaller:
    """插件安装器

    支持多种 source 类型：
    - git-subdir（DriFox）：{"type": "git-subdir", "url": "...", "path": "...", "ref": "..."}
    - github（Claude Code）：{"source": "github", "repo": "owner/repo", "ref": "..."}
    - url（Claude Code）：{"source": "url", "url": "https://...", "ref": "..."}
    - 相对路径："./plugins/xxx"（暂不支持自动安装，仅本地市场有效）
    """

    def __init__(self):
        drifox = _drifox_dir()
        self._plugins_dir = drifox / "plugins"
        self._disabled_dir = drifox / "plugins-disabled"
        self._cache_dir = drifox / "cache" / "install_tmp"
        # 项目根 plugins/ 目录（系统插件）
        self._system_dir = Path(__file__).resolve().parent.parent.parent.parent / "plugins"
        # 已安装插件状态缓存（{name: version|None}），TTL 防频繁磁盘扫描
        self._inst_map_cache: Optional[dict] = None
        self._inst_map_ts: float = 0.0
        # 插件状态缓存（{name: "enabled"|"disabled"|"system"}）
        self._status_map_cache: Optional[dict] = None
        self._status_map_ts: float = 0.0
        # manifest 缓存（{name: manifest dict}，get_installed_map 扫描时填充，
        # 供 UI 图标/详情复用，避免重复读盘）
        self._manifest_cache: dict = {}

    # ── 批量状态查询 ──────────────────────────────────────

    _INST_MAP_TTL = 3.0  # 秒：渲染/筛选频繁调用时避免重复扫描磁盘

    def get_installed_map(self, use_cache: bool = True) -> dict:
        """批量扫描已安装插件，返回 {插件名: 版本号或 None}

        扫描范围：用户启用（.drifox/plugins）+ 用户禁用（.drifox/plugins-disabled）
        + 系统插件（项目根 plugins/）。系统插件仅当与用户插件不重名时计入，
        避免覆盖用户同名插件的版本号。

        Args:
            use_cache: 是否使用短 TTL 缓存（安装/更新/卸载后应传 False 或调用失效）

        Returns:
            {name: version_or_None}，version 读取失败为 None
        """
        now = time.time()
        if use_cache and self._inst_map_cache is not None and now - self._inst_map_ts < self._INST_MAP_TTL:
            return self._inst_map_cache

        result: dict = {}
        manifest_cache: dict = {}
        for base in (self._plugins_dir, self._disabled_dir):
            if base.is_dir():
                try:
                    for entry in os.scandir(base):
                        if not entry.is_dir():
                            continue
                        version, manifest = self._read_version_full(Path(entry.path))
                        result[entry.name] = version
                        manifest_cache[entry.name] = manifest
                except OSError:
                    pass
        # 系统插件（项目根 plugins/）：与用户插件重名时跳过
        if self._system_dir.is_dir():
            try:
                for entry in os.scandir(self._system_dir):
                    if not entry.is_dir() or entry.name in result:
                        continue
                    version, manifest = self._read_version_full(Path(entry.path))
                    result[entry.name] = version
                    manifest_cache[entry.name] = manifest
            except OSError:
                pass

        self._manifest_cache = manifest_cache
        self._inst_map_cache = result
        self._inst_map_ts = now
        return result

    def get_status_map(self, use_cache: bool = True) -> dict:
        """批量扫描插件状态，返回 {插件名: "enabled"|"disabled"|"system"}

        与 get_installed_map 的扫描范围一致：
        - .drifox/plugins → enabled
        - .drifox/plugins-disabled → disabled
        - 项目根 plugins/（不重名）→ system
        """
        now = time.time()
        if use_cache and self._status_map_cache is not None and now - self._status_map_ts < self._INST_MAP_TTL:
            return self._status_map_cache

        result: dict = {}
        if self._plugins_dir.is_dir():
            try:
                for entry in os.scandir(self._plugins_dir):
                    if entry.is_dir():
                        result[entry.name] = "enabled"
            except OSError:
                pass
        if self._disabled_dir.is_dir():
            try:
                for entry in os.scandir(self._disabled_dir):
                    if entry.is_dir():
                        result[entry.name] = "disabled"
            except OSError:
                pass
        if self._system_dir.is_dir():
            try:
                for entry in os.scandir(self._system_dir):
                    if entry.is_dir() and entry.name not in result:
                        result[entry.name] = "system"
            except OSError:
                pass

        self._status_map_cache = result
        self._status_map_ts = now
        return result

    @staticmethod
    def _read_version_full(plugin_dir: Path) -> Tuple[Optional[str], Optional[dict]]:
        """读取版本号 + manifest（一次 IO，供版本与图标/详情复用）"""
        for sub in (".drifox-plugin", ".claude-plugin"):
            mp = plugin_dir / sub / "plugin.json"
            if mp.exists():
                try:
                    manifest = json.loads(mp.read_text(encoding="utf-8"))
                    return manifest.get("version"), manifest
                except Exception:
                    return None, None
        return None, None

    def invalidate_installed_cache(self):
        """安装/更新/卸载后使缓存失效，保证下次查询拿到最新状态"""
        self._inst_map_cache = None
        self._inst_map_ts = 0.0
        self._status_map_cache = None
        self._status_map_ts = 0.0
        self._manifest_cache = {}

    # ── 安装 ─────────────────────────────────────────────

    def install(self, plugin_meta: dict) -> bool:
        """安装插件（自动识别 source 类型）

        Args:
            plugin_meta: marketplace.json 中的插件元数据

        Returns:
            True 安装成功
        """
        source = plugin_meta.get("source", {})
        name = plugin_meta.get("name", "")
        if not name:
            return False

        target = self._plugins_dir / name
        if target.exists():
            logger.info(f"[Installer] Plugin {name} already exists, skipping install")
            return True

        success = self._install_by_source(name, source, target, plugin_meta.get("_marketplace_source"))
        if success:
            self.invalidate_installed_cache()
        return success

    def update(self, plugin_meta: dict) -> bool:
        """更新插件 — 立即删除旧版后重新下载

        流程（点击更新即触发）：
        1. 先删除旧版目录（用户感知为「点了立即删除现有的」）
        2. 再下载新版安装
        若下载失败，插件保持未安装状态（可重新点「安装」恢复）。

        Args:
            plugin_meta: marketplace.json 中的插件元数据

        Returns:
            True 更新成功
        """
        name = plugin_meta.get("name", "")
        if not name:
            return False

        source = plugin_meta.get("source", {})
        target = self._plugins_dir / name
        remote_ver = plugin_meta.get("version", "0.0.0")

        # 第一步：立即删除旧版
        if not self.remove(name):
            logger.error(f"[Installer] Failed to remove old {name} before update")
            return False

        # 第二步：重新下载安装
        success = self._install_by_source(name, source, target, plugin_meta.get("_marketplace_source"))
        if success:
            logger.info(f"[Installer] Updated plugin {name} -> v{remote_ver}")
        return success

    # ── Source 类型分发 ──────────────────────────────────

    def _install_by_source(self, name: str, source, target: Path, marketplace_source: dict = None) -> bool:
        """根据 source 类型分发安装逻辑"""
        # 字符串 source → 相对路径
        if isinstance(source, str):
            if source.startswith("./"):
                return self._install_relative(name, source, target, marketplace_source)
            else:
                logger.error(f"[Installer] 不支持的 source 格式: {source}")
            return False

        if not isinstance(source, dict):
            logger.error(f"[Installer] 不支持的 source 类型: {type(source)}")
            return False

        # 识别 source 类型：优先 Claude Code 的 "source" 字段，其次 DriFox 的 "type" 字段
        src_type = source.get("source") or source.get("type", "")

        if src_type == "github":
            return self._install_github(name, source, target)
        elif src_type == "url":
            return self._install_git_url(name, source, target)
        elif src_type == "git-subdir":
            return self._install_git_subdir(name, source, target)
        else:
            logger.error(f"[Installer] 不支持的 source 类型: {src_type}")
            return False

    def _install_github(self, name: str, source: dict, target: Path) -> bool:
        """从 GitHub 仓库安装插件（Claude Code 格式）"""
        repo = source.get("repo", "")
        if not repo:
            return False
        ref = source.get("ref", "main")
        url = _resolve_github_url(repo)
        # 整个仓库就是插件
        return self._download_and_move(name, url, ".", ref, target)

    def _install_git_url(self, name: str, source: dict, target: Path) -> bool:
        """从 Git URL 安装插件（Claude Code 格式）"""
        url = source.get("url", "")
        if not url:
            return False
        ref = source.get("ref", "main")
        # url 类型通常整个仓库就是插件
        return self._download_and_move(name, url, ".", ref, target)

    def _install_git_subdir(self, name: str, source: dict, target: Path) -> bool:
        """从 git-subdir 类型安装插件（兼容 DriFox 旧格式）"""
        raw_url = source.get("url", "")
        url = _resolve_git_subdir_url(raw_url)
        subpath = source.get("path", "")
        ref = source.get("ref", "main")
        if not url or not subpath:
            return False
        return self._download_and_move(name, url, subpath, ref, target)

    def _install_relative(self, name: str, relative_path: str, target: Path, marketplace_source: dict = None) -> bool:
        """从相对路径安装插件（从市场仓库中提取子目录）

        将相对路径转换为 git-subdir 安装：
        - 相对路径如 "./plugin-builder" 或 "./plugins/xxx"
        - 从 marketplace_source 推断仓库 URL
        - 使用 sparse clone 只下载该子目录
        """
        if not marketplace_source:
            logger.warning(f"[Installer] 无法安装相对路径插件 {name}：缺少市场源信息")
            return False

        # 去掉 "./" 前缀得到子目录路径
        subpath = relative_path.lstrip("./")
        if not subpath:
            logger.error(f"[Installer] 无效的相对路径: {relative_path}")
            return False

        src_type = marketplace_source.get("source", "")
        ref = marketplace_source.get("ref", "main")

        if src_type == "github":
            repo = marketplace_source.get("repo", "")
            url = _resolve_github_url(repo)
        elif src_type == "url":
            url = marketplace_source.get("url", "")
            # 如果是 raw URL (marketplace.json 直链)，无法 clone
            if "/raw.githubusercontent.com/" in url or url.endswith(".json"):
                logger.warning(f"[Installer] 无法从 URL 类型市场安装相对路径插件: {url}")
                return False
        else:
            logger.warning(f"[Installer] 不支持的市场源类型用于相对路径: {src_type}")
            return False

        if not url:
            return False

        logger.info(f"[Installer] 相对路径安装: {name} ← {url} / {subpath}")
        return self._download_and_move(name, url, subpath, ref, target)

    # ── 核心下载逻辑 ─────────────────────────────────────

    def _download_and_move(self, name: str, url: str, subpath: str, ref: str, target: Path) -> bool:
        """从 git 源下载插件并移动到目标目录

        Args:
            name: 插件名
            url: git 仓库 URL
            subpath: 仓库内子目录路径（"." 表示整个仓库）
            ref: 分支/标签
            target: 目标安装目录
        """
        try:
            self._plugins_dir.mkdir(parents=True, exist_ok=True)
            self._cache_dir.mkdir(parents=True, exist_ok=True)

            # === 1. 下载到 cache 目录 ===
            cache_tmp = self._cache_dir / f"{name}_{int(time.time())}"
            cache_tmp.mkdir(parents=True, exist_ok=True)
            try:
                self._sparse_clone(url, subpath, ref, cache_tmp)
            except Exception:
                shutil.rmtree(cache_tmp, ignore_errors=True)
                raise

            # === 2. 确定源目录 ===
            if subpath in (".", ""):
                sub_src = cache_tmp
            else:
                sub_src = cache_tmp / subpath
            if not sub_src.exists():
                shutil.rmtree(cache_tmp, ignore_errors=True)
                raise RuntimeError(f"Subpath {subpath} not found in clone")

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sub_src), str(target))

            # === 3. 清理 cache ===
            shutil.rmtree(cache_tmp, ignore_errors=True)

            logger.info(f"[Installer] Installed plugin {name} -> {target}")
            # 安装/更新成功 → 上报下载量（后台线程，失败不影响安装结果）
            report_plugin_install(name)
            return True

        except Exception as e:
            logger.error(f"[Installer] Download {name} failed: {e}")
            return False

    def _sparse_clone(self, url: str, subpath: str, ref: str, cache_dir: Path):
        """克隆仓库指定子目录到 cache_dir

        对 subpath="." 的情况，全量浅克隆（不用 sparse-checkout）。
        """
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        if subpath in (".", ""):
            # 整个仓库：直接浅克隆
            subprocess.run(
                ["git", "clone", "--depth=1", "--single-branch", "--branch", ref, url, str(cache_dir)],
                check=True,
                capture_output=True,
                text=True,
                **kwargs,
            )
        else:
            # 子目录：稀疏克隆
            subprocess.run(
                ["git", "clone", "--depth=1", "--filter=blob:none", "--sparse", url, str(cache_dir)],
                check=True,
                capture_output=True,
                text=True,
                **kwargs,
            )
            subprocess.run(
                ["git", "-C", str(cache_dir), "sparse-checkout", "set", subpath],
                check=True,
                capture_output=True,
                text=True,
                **kwargs,
            )

    # ── 卸载前释放引用 ──────────────────────────────────

    @staticmethod
    def _purge_plugin_module_cache(name: str):
        """删除/移动插件目录前，清理该插件的模块缓存（线程安全，无 GUI 操作）

        UI 插件（含 ui/ 组件的插件）被加载后，其模块（ui_plugin_xxx）会常驻
        sys.modules，模块对象可能持有 .pyc 句柄，导致插件目录在 Windows 上
        无法 rmtree 或 move（WinError 32）。

        注意：本方法只做纯数据清理，不触碰任何 Qt 控件 —— UI 插件注册表
        的卸载（含显示中卡片的自动关闭、widget 销毁）必须在主线程完成，
        由调用方（marketplace 卡片）在启动后台线程前先执行
        UIPluginRegistry.unload_plugin(name)。

        本方法执行：
        1. 从 sys.modules 移除 ui_plugin_{name} 及其子模块 —— 解除 .pyc 句柄
        2. importlib.invalidate_caches() + gc.collect() —— 失效字节码缓存并回收残留引用
        3. 删除残留的 ui/__pycache__ —— 兜底清理可能被句柄占用的 .pyc 文件

        任何一步失败都不阻塞删除流程（宁可删除失败后重试，也不因清理失败中断）。
        """
        try:
            import gc
            import importlib
            import sys

            safe = name.replace("-", "_").replace(":", "_")
            head = f"ui_plugin_{safe}"
            for mod_name in list(sys.modules.keys()):
                if mod_name == head or mod_name.startswith(head + "."):
                    del sys.modules[mod_name]
            importlib.invalidate_caches()
            gc.collect()
        except Exception as e:
            logger.debug(f"[Installer] 清理 sys.modules 失败（忽略）: {e}")

        # 兜底：删除插件可能的 __pycache__ 残留（句柄释放后再删一般能成功）
        for base in (_drifox_dir() / "plugins", _drifox_dir() / "plugins-disabled"):
            pc = base / name / "ui" / "__pycache__"
            if pc.exists():
                _rmtree_readonly(pc)

    # ── 卸载 ─────────────────────────────────────────────

    def remove(self, name: str) -> bool:
        """删除插件目录（更新/卸载共用）

        Args:
            name: 插件名

        Returns:
            True 删除成功或目录不存在
        """
        # 清理模块缓存（UI 注册表卸载由 GUI 线程在主线程先行完成）
        self._purge_plugin_module_cache(name)
        removed = False
        for base in (self._plugins_dir, self._disabled_dir):
            target = base / name
            if target.exists():
                if _rmtree_readonly(target):
                    removed = True
                    logger.info(f"[Installer] Removed plugin {name}")
                else:
                    return False
        if removed:
            self.invalidate_installed_cache()
        return True

    def uninstall(self, name: str) -> bool:
        """卸载插件（删除本地目录）"""
        if not self.remove(name):
            return False
        logger.info(f"[Installer] Uninstalled plugin {name}")
        return True

    # ── 启用 / 禁用 ──────────────────────────────────────

    def disable(self, name: str) -> bool:
        """禁用已启用的用户插件（移动到 plugins-disabled 目录）

        系统插件（项目根 plugins/）不可禁用。
        """
        src = self._plugins_dir / name
        if not src.exists():
            return False
        # 清理模块缓存（UI 注册表卸载由主线程先行完成，此处不碰 GUI）
        self._purge_plugin_module_cache(name)
        try:
            self._disabled_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(self._disabled_dir / name))
            self.invalidate_installed_cache()
            logger.info(f"[Installer] Disabled plugin {name}")
            return True
        except Exception as e:
            logger.error(f"[Installer] Disable {name} failed: {e}")
            return False

    def enable(self, name: str) -> bool:
        """启用已禁用的插件（从 plugins-disabled 移回 plugins）"""
        src = self._disabled_dir / name
        if not src.exists():
            return False
        try:
            self._plugins_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(self._plugins_dir / name))
            self.invalidate_installed_cache()
            logger.info(f"[Installer] Enabled plugin {name}")
            return True
        except Exception as e:
            logger.error(f"[Installer] Enable {name} failed: {e}")
            return False

    # ── 状态查询 ─────────────────────────────────────────

    def is_installed(self, name: str) -> bool:
        """检查插件是否已安装（走批量缓存，避免逐次磁盘 IO）"""
        return name in self.get_installed_map()

    def get_installed_version(self, name: str) -> Optional[str]:
        """读取已安装插件的本地版本号（走批量缓存）

        Args:
            name: 插件名称

        Returns:
            版本号字符串，如 "1.0.0"，如果未安装或读取失败返回 None
        """
        return self.get_installed_map().get(name)

    def check_update(self, plugin_meta: dict) -> Tuple[bool, Optional[str], Optional[str]]:
        """检查插件是否有可用更新

        Args:
            plugin_meta: marketplace.json 中的插件元数据

        Returns:
            (has_update: bool, local_version: Optional[str], remote_version: Optional[str])
        """
        name = plugin_meta.get("name", "")
        if not name:
            return (False, None, None)

        local_ver = self.get_installed_version(name)
        if local_ver is None:
            return (False, None, None)

        remote_ver = plugin_meta.get("version")
        if not remote_ver:
            return (False, local_ver, None)

        has_update = compare_versions(local_ver, remote_ver) < 0
        return (has_update, local_ver, remote_ver)


_instance: Optional[PluginInstaller] = None


def get_installer() -> PluginInstaller:
    global _instance
    if _instance is None:
        _instance = PluginInstaller()
    return _instance
