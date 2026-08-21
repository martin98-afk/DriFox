# -*- coding: utf-8 -*-
"""插件安装器 — 通过 git 拉取市场插件

设计要点：
- 临时下载到 cache 目录，避免触发 watchfiles 插件热更新
- 下载完成后再原子式 move 到 plugins 目录
- 支持版本检测与更新
- 支持多种 source 类型（兼容 Claude Code marketplace 格式）
"""

import ctypes
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
from .proxy import get_proxy_config


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


def _rmtree_readonly(path: Path, failures_out: list = None) -> bool:
    """强制删除目录树（含只读文件）

    git clone 生成的 ``.git/objects/pack/*.idx|pack|rev`` 默认带只读属性，
    ``shutil.rmtree`` 不会清除该属性，在 Windows 上调用 ``os.unlink`` 会
    抛出 ``WinError 5 拒绝访问``。本方法在 ``onexc`` 回调里先 chmod 清除
    只读属性，再重试删除。

    Windows 上被进程加载的 .pyd/.dll（如 gateway 插件 deps 的 Crypto）即使
    chmod 也无法删除；onexc 中重试失败会记录到 failures，**不静默吞掉**，
    调用方可据此感知"部分残留"并走兜底（pending-delete 下次启动清理）。

    Args:
        path: 要删除的目录
        failures_out: 可选；传入 list 时把删除失败的路径追加进去（供调用方
            感知具体残留，用于 pending-delete 记录）

    Returns:
        True 全部删除成功（或目录不存在）；False 有文件残留或整体失败
    """
    failures: list = [] if failures_out is None else failures_out

    if not path.exists():
        return True

    def _onerror(func, failed_path, excinfo):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except OSError:
            failures.append(str(failed_path))

    try:
        # Python 3.12+ 新 API；项目要求 3.14+
        shutil.rmtree(path, onexc=_onerror)
    except Exception as e:
        logger.error(f"[Installer] rmtree {path} failed: {e}")
        failures.append(str(path))
        return False
    if failures_out is None and failures:
        logger.warning(f"[Installer] rmtree {path} 残留 {len(failures)} 个文件（可能被占用）: {failures[:5]}")
        return False
    return True


def _move_file_delayed_delete(path_str: str) -> None:
    """排队在下次系统重启时删除文件（MoveFileExW + MOVEFILE_DELAY_UNTIL_REBOOT）

    仅 Windows 可用；被其它进程永久独占、连 rename 都失败时的最后兜底。
    可能因权限（需管理员/SE_RESTORE_NAME）失败，调用方应吞掉异常交回 pending-delete。
    """
    if sys.platform != "win32":
        raise OSError("仅 Windows 支持重启删除")
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
    ok = kernel32.MoveFileExW(ctypes.c_wchar_p(path_str), None, MOVEFILE_DELAY_UNTIL_REBOOT)
    if not ok:
        raise ctypes.WinError()  # type: ignore[attr-defined]


def _relocate_locked_file(installer: "PluginInstaller", src: Path) -> bool:
    """把被进程占用（已加载的原生 .pyd/.dll）文件改名移出插件目录。

    Windows 上已加载的原生模块在进程存活期间**无法删除**，但其目录项可被同卷
    rename（映射视图不受影响），故 move 到隔离目录通常成功，从而把插件目录让空、
    解决「删不掉也装不上」的死锁。rename 仍失败（被其它进程独占）时退化到
    MoveFileEx(MOVEFILE_DELAY_UNTIL_REBOOT) 排队重启删除。
    """
    try:
        qdir: Path = installer._quarantine_dir
        qdir.mkdir(parents=True, exist_ok=True)
        # 保留相对层级，避免跨插件同名 .pyd 冲突（如多个 gateway 都 vendor Crypto）
        try:
            if src.is_relative_to(installer._plugins_dir):
                rel = src.relative_to(installer._plugins_dir)
            elif src.is_relative_to(installer._disabled_dir):
                rel = src.relative_to(installer._disabled_dir)
            else:
                rel = Path(src.name)
        except Exception:
            rel = Path(src.name)
        dst = qdir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        logger.info(f"[Installer] 占用文件已移出插件目录（重启后清理）: {src} -> {dst}")
        return True
    except Exception as e:
        try:
            _move_file_delayed_delete(str(src))
            logger.info(f"[Installer] 占用文件已排队重启删除: {src}")
            return True
        except Exception as e2:
            logger.debug(f"[Installer] 占用文件无法移出: {src}: {e} | {e2}")
            return False


def _rmtree_relocate(installer: "PluginInstaller", path: Path, failures_out: Optional[list] = None) -> bool:
    """rmtree 失败时把被占用的 .pyd/.dll 改名移出，再二次 rmtree 清空。

    解决 Windows 下已加载原生模块无法删除、导致插件目录删不掉也装不上的死锁。
    Returns: True 目标已清空/消失（占用文件已被移出隔离目录）。
    """
    failures: list = []
    if _rmtree_readonly(path, failures):
        return True
    # 第一轮失败：把锁定的文件（多为 .pyd/.dll）改名移出插件目录
    for f in list(failures):
        fp = Path(f)
        if fp.is_file() and _relocate_locked_file(installer, fp):
            failures.remove(f)
    # 文件移出后，残留多为空目录，二次 rmtree 清掉
    if path.exists():
        rem: list = []
        if _rmtree_readonly(path, rem):
            return True
        failures.extend(rem)
    else:
        return True
    if failures_out is not None:
        failures_out.extend(sorted(set(failures)))
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
        # 被占用（已加载原生 .pyd/.dll）文件的隔离目录：rmtree 删不掉时
        # 把文件同卷 rename 到这里，让插件目录让空；重启后由 _cleanup_pending_delete 清掉。
        self._quarantine_dir = drifox / ".plugin_quarantine"
        # 项目根 plugins/ 目录（系统插件）
        self._system_dir = Path(__file__).resolve().parent.parent.parent.parent / "plugins"
        # 已安装插件状态缓存（{name: version|None}），TTL 防频繁磁盘扫描
        self._inst_map_cache: Optional[dict] = None
        self._inst_map_ts: float = 0.0
        # 插件状态缓存（{name: "enabled"|"disabled"|"system"|"builtin_enabled"|"builtin_disabled"}）
        self._status_map_cache: Optional[dict] = None
        self._status_map_ts: float = 0.0
        # manifest 缓存（{name: manifest dict}，get_installed_map 扫描时填充，
        # 供 UI 图标/详情复用，避免重复读盘）
        self._manifest_cache: dict = {}
        # 最近一次安装/更新失败原因（供 UI 记录区展示；成功/未执行时为空）
        self.last_error: str = ""
        # 清理上次卸载残留（进程重启后 DLL/.pyd 句柄已释放，可正常删除）
        self._cleanup_pending_delete()

    # 缓存字段组锁：worker 线程写（invalidate/预填充）、GUI 主线程读
    # （get_installed_map/get_status_map）。多字段（_inst_map_cache /
    # _inst_map_ts / _manifest_cache / _status_map_cache / _status_map_ts）
    # 非原子更新，锁保证读方看不到"半写"中间态。类级默认：兼容测试用
    # PluginInstaller.__new__ 手赋属性的构造方式（不执行 __init__）。
    _cache_lock = threading.Lock()

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
        with self._cache_lock:
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
                        if manifest is None:
                            # 非插件目录（卸载残留/无关文件夹）：无 plugin.json
                            # 不视为已安装插件，避免市场显示"禁用/卸载"残留。
                            continue
                        result[entry.name] = version
                        manifest_cache[entry.name] = manifest
                except OSError:
                    pass
        # 系统插件（项目根 plugins/）：与用户插件重名时跳过。
        # getattr 兜底：兼容测试用 __new__ 手赋属性的构造（无 _system_dir 时跳过）。
        system_dir = getattr(self, "_system_dir", None)
        if system_dir is not None and system_dir.is_dir():
            try:
                for entry in os.scandir(system_dir):
                    if not entry.is_dir() or entry.name in result:
                        continue
                    version, manifest = self._read_version_full(Path(entry.path))
                    if manifest is None:
                        continue
                    result[entry.name] = version
                    manifest_cache[entry.name] = manifest
            except OSError:
                pass

        with self._cache_lock:
            self._manifest_cache = manifest_cache
            self._inst_map_cache = result
            self._inst_map_ts = now
        return result

    def get_status_map(self, use_cache: bool = True) -> dict:
        """批量扫描插件状态，返回 {插件名: "enabled"|"disabled"|"system"|"builtin_enabled"|"builtin_disabled"}

        与 get_installed_map 的扫描范围一致：
        - .drifox/plugins → enabled
        - .drifox/plugins-disabled → disabled
        - 项目根 plugins/（不重名）→ 按 manifest type 区分：
          type=system → system（真系统插件）；type!=system → builtin_enabled/
          builtin_disabled（内置非 system 插件，按 Settings.disabled_plugins 判定）
        """
        now = time.time()
        with self._cache_lock:
            if use_cache and self._status_map_cache is not None and now - self._status_map_ts < self._INST_MAP_TTL:
                return self._status_map_cache

        result: dict = {}
        if self._plugins_dir.is_dir():
            try:
                for entry in os.scandir(self._plugins_dir):
                    if entry.is_dir() and self._read_manifest_at(Path(entry.path)) is not None:
                        result[entry.name] = "enabled"
            except OSError:
                pass
        if self._disabled_dir.is_dir():
            try:
                for entry in os.scandir(self._disabled_dir):
                    if entry.is_dir() and self._read_manifest_at(Path(entry.path)) is not None:
                        result[entry.name] = "disabled"
            except OSError:
                pass
        # getattr 兜底：兼容测试用 __new__ 手赋属性的构造（无 _system_dir 时跳过）。
        system_dir = getattr(self, "_system_dir", None)
        if system_dir is not None and system_dir.is_dir():
            disabled_set = self._load_disabled_names()
            try:
                for entry in os.scandir(system_dir):
                    if not entry.is_dir() or entry.name in result:
                        continue
                    # 按 manifest type 区分：type=system → 真系统插件（仅更新）；
                    # 其余（type=user/缺失）→ 内置非 system 插件，可禁用/启用
                    manifest = self._read_manifest_at(Path(entry.path))
                    if manifest is not None and manifest.get("type") == "system":
                        result[entry.name] = "system"
                    else:
                        result[entry.name] = "builtin_disabled" if entry.name in disabled_set else "builtin_enabled"
            except OSError:
                pass

        with self._cache_lock:
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

    @staticmethod
    def _read_manifest_at(plugin_dir: Path) -> Optional[dict]:
        """读取插件目录 manifest（.drifox-plugin 优先，其次 .claude-plugin），失败返回 None"""
        for sub in (".drifox-plugin", ".claude-plugin"):
            mp = plugin_dir / sub / "plugin.json"
            if mp.exists():
                try:
                    return json.loads(mp.read_text(encoding="utf-8"))
                except Exception:
                    return None
        return None

    @staticmethod
    def _load_disabled_names() -> set:
        """从 Settings 读取禁用插件集合（内置插件禁用判定）"""
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            return set(cfg.disabled_plugins.value or [])
        except Exception:
            return set()

    def _set_managed_enabled(self, name: str, enabled: bool) -> bool:
        """通过 PluginManager 启用/禁用内置插件（写 Settings + 联动 UI/工具注册）

        仅内置非 system 插件（项目根 plugins/ 且 manifest type != system）使用：
        不移动文件（目录随主程序分发），改由 PluginManager.disable_plugin /
        enable_plugin 写 enabled_plugins/disabled_plugins 配置并联动卸载/加载
        UI 组件、重扫工具注册。_iter_enabled_plugins 按 enabled 集过滤，
        禁用后该插件资源自动不加载。

        在后台 worker 线程调用：disable_plugin 内部 _unload_plugin_ui 对
        UIPluginRegistry 幂等（UI 已由 cards 主线程先行卸载/未加载时直接返回），
        不触碰 Qt 控件。
        """
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if pm.get_plugin(name) is None:
                logger.warning(f"[Installer] PluginManager 中不存在插件，无法{'启用' if enabled else '禁用'}: {name}")
                return False
            if enabled:
                pm.enable_plugin(name)
            else:
                pm.disable_plugin(name)
            self.invalidate_installed_cache()
            logger.info(f"[Installer] {'Enabled' if enabled else 'Disabled'} builtin plugin {name} (settings)")
            return True
        except Exception as e:
            logger.error(f"[Installer] set managed {'enable' if enabled else 'disable'} {name} failed: {e}")
            return False

    def invalidate_installed_cache(self):
        """使缓存失效（清空后立即在调用线程重填，语义=清后立即重填）

        调用方均为后台安装/更新/卸载 worker 线程（install/update/remove/
        disable/enable 成功路径）。清空后立刻在本线程触发一次全量扫描填充
        TTL 缓存：主线程完成回调（_on_install_done → _refresh_row_states）
        在 3s TTL 窗口内命中缓存，跳过全量磁盘扫描（消灭安装/卸载后
        主线程 os.scandir + 逐插件读 manifest 的卡顿根因）。

        若未来有主线程调用本方法，重填扫描会在主线程同步执行——
        调用方需保证本方法只从后台线程进入。
        """
        with self._cache_lock:
            self._inst_map_cache = None
            self._inst_map_ts = 0.0
            self._status_map_cache = None
            self._status_map_ts = 0.0
            self._manifest_cache = {}
        # 清空后立即重填（仍在后台线程）：get_installed_map 填充 inst+manifest
        # 缓存，get_status_map 填充 status 缓存。任一失败不阻塞（主线程回调
        # 有 TTL miss 兜底自行扫描）。
        try:
            self.get_installed_map(use_cache=False)
            self.get_status_map(use_cache=False)
        except Exception as e:
            logger.warning(f"[Installer] 缓存预填充失败（主线程回调将兜底扫描）: {e}")

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
            if self._read_manifest_at(target) is not None:
                logger.info(f"[Installer] Plugin {name} already exists, skipping install")
                return True
            # 残留目录（卸载不净/无关文件夹，无 plugin.json）→ 不视为已安装，
            # 先清理再安装，否则点「安装」会一直跳过、永远装不上。
            logger.info(f"[Installer] 插件 {name} 存在残留目录（无 manifest），先清理再安装")
            # 等待 adapter 完全停止 + 摘除 registry def，释放 SDK/.pyd 句柄
            self._stop_gateway_platforms(name, wait=True)
            self._purge_plugin_module_cache(name)
            # 被进程加载的 .pyd/.dll 无法在运行中删除：_rmtree_relocate 会把它们
            # 同卷 rename 到隔离目录（让空插件目录），再二次 rmtree 清空；
            # 仍失败则 gc + 短暂等待后重试（给 stop 协程/GC 释放句柄的窗口）。
            import gc

            cleaned = False
            for _attempt in range(3):
                if _rmtree_relocate(self, target):
                    cleaned = True
                    break
                gc.collect()
                time.sleep(0.5)
            if not cleaned:
                logger.warning(f"[Installer] 残留目录清理失败（文件被占用），安装中止: {target}")
                return False

        success = self._install_by_source(name, source, target, plugin_meta.get("_marketplace_source"))
        if success:
            self.last_error = ""
            self.invalidate_installed_cache()
        else:
            if not self.last_error:
                self.last_error = f"Install {name} failed"
        return success

    def update(self, plugin_meta: dict) -> bool:
        """更新插件 — 先下载新版，下载成功后再替换旧版（失败保留旧版）

        流程（点击更新即触发）：
        1. 下载新版到 cache 目录（不触碰旧版；网络失败时旧版完好可用）
        2. 下载成功 → 旧版备份移入 cache → 新版移入目标目录 → 删除备份
        替换环节失败 → 回滚旧版，插件保持可用状态（不出现
        「旧版已删、新版未装」的未安装窗口）。

        禁用中的插件（目录在 plugins-disabled/）原地更新：目标目录指向
        禁用目录，更新后插件仍保持禁用（修复此前更新把新版装到 plugins/
        导致双副本 + 禁用状态被破坏的问题）。

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
        # 禁用中的插件（目录在 plugins-disabled/）原地更新：目标指向禁用目录，
        # 保持禁用状态（修复此前新版装到 plugins/ 导致双副本 + 禁用失效）。
        # getattr 兜底：兼容测试用 __new__ 手赋属性的构造（无 _disabled_dir 时跳过）
        disabled_dir = getattr(self, "_disabled_dir", None)
        if not target.exists() and disabled_dir is not None and (disabled_dir / name).exists():
            target = disabled_dir / name
        elif not target.exists():
            # 内置插件（项目根 plugins/<name> 目录存在，含真系统 type=system 与
            # builtin 非 system type=user/缺失）→ 原地覆盖项目目录，避免双副本。
            # 更新完成后由 cards 主线程回调 rescan_plugin 热重载（含 UI 组件）。
            system_dir = getattr(self, "_system_dir", None)
            if system_dir is not None and (system_dir / name).is_dir():
                target = system_dir / name
        remote_ver = plugin_meta.get("version", "0.0.0")

        # 目标目录已存在 → 走 _download_and_move 的备份替换分支；
        # 不存在（更新前被手动删除）→ 等同安装。
        success = self._install_by_source(name, source, target, plugin_meta.get("_marketplace_source"))
        if success:
            logger.info(f"[Installer] Updated plugin {name} -> v{remote_ver}")
            self.last_error = ""
            self.invalidate_installed_cache()
        else:
            if not self.last_error:
                self.last_error = f"Update {name} failed"
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
            self._suppress_backend_watcher()
            self._plugins_dir.mkdir(parents=True, exist_ok=True)
            self._cache_dir.mkdir(parents=True, exist_ok=True)

            # === 1. 下载到 cache 目录 ===
            cache_tmp = self._cache_dir / f"{name}_{int(time.time())}"
            cache_tmp.mkdir(parents=True, exist_ok=True)
            try:
                proxy = get_proxy_config()
                git_url, git_extra = proxy.git_clone_args(url)
                candidates = self._build_clone_candidates(url, git_url, git_extra, proxy)
                last_err: Optional[Exception] = None
                for i, (cu, ce) in enumerate(candidates):
                    if i > 0:
                        logger.info(f"[Installer] retry #{i}: {cu}")
                    shutil.rmtree(cache_tmp, ignore_errors=True)
                    cache_tmp.mkdir(parents=True, exist_ok=True)
                    try:
                        self._sparse_clone(cu, subpath, ref, cache_tmp, extra_args=ce)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        logger.warning(f"[Installer] attempt #{i} failed ({cu}): {self._format_git_err(e)}")
                if last_err is not None:
                    # 所有候选都失败：抛最后一个错误（带 stderr），让外层 logger.error 记录
                    raise last_err
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
            if target.exists():
                # 更新场景：旧版已在 → 先备份到 cache，新版落位成功后再删
                # 旧版；替换失败回滚旧版（下载已完成，失败只可能来自本地
                # IO/句柄，回滚保证插件不落入「旧版已删、新版未装」状态）。
                # 备份在 cache 目录（.drifox/cache），不触发插件目录监听。
                backup = self._cache_dir / f"{name}_old_{int(time.time())}"
                # 释放旧目录的模块/字节码句柄（Windows 下 move 可能失败）
                self._purge_plugin_module_cache(name)
                self._robust_move(str(target), str(backup))
                try:
                    self._robust_move(str(sub_src), str(target))
                except Exception:
                    # 替换失败 → 回滚旧版
                    try:
                        if backup.exists() and not target.exists():
                            shutil.move(str(backup), str(target))
                    except Exception as rb_e:
                        logger.error(f"[Installer] 回滚旧版失败: {name}: {rb_e}")
                    shutil.rmtree(cache_tmp, ignore_errors=True)
                    raise
                if not _rmtree_readonly(backup):
                    logger.warning(f"[Installer] 旧版备份清理失败（残留 cache）: {backup}")
            else:
                self._robust_move(str(sub_src), str(target))

            # === 3. 清理 cache ===
            shutil.rmtree(cache_tmp, ignore_errors=True)

            logger.info(f"[Installer] Installed plugin {name} -> {target}")
            # 安装/更新成功 → 上报下载量（后台线程，失败不影响安装结果）
            report_plugin_install(name)
            # 恢复 backend watcher 并精准重载目标插件（仅加载该插件，不触发全量）
            self._resume_backend_watcher(reload=True, plugin_name=name)
            return True

        except Exception as e:
            # 恢复 backend watcher（失败不重载），并清理下载缓存与半安装残骸
            try:
                self._resume_backend_watcher(reload=False)
            except Exception:
                pass
            try:
                if "cache_tmp" in dir() and cache_tmp.exists():
                    shutil.rmtree(cache_tmp, ignore_errors=True)
            except Exception:
                pass
            self.last_error = self._format_git_err(e)
            logger.error(f"[Installer] Download {name} failed: {self.last_error}")
            return False

    def _sparse_clone(self, url: str, subpath: str, ref: str, cache_dir: Path, extra_args: Optional[list] = None):
        """克隆仓库指定子目录到 cache_dir

        对 subpath="." 的情况，全量浅克隆（不用 sparse-checkout）。
        extra_args: git 前置参数（如 ['-c', 'http.proxy=...']），
        插在 git 与子命令之间。

        失败时把 git 的 stderr 一起抛（不要直接 ``check=True`` —— 那样
        ``CalledProcessError.stderr`` 是 ``None``，外层日志看不到真实错误）。
        """
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        extra = list(extra_args or [])

        def _run(cmd):
            result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode,
                    result.args,
                    output=result.stdout,
                    stderr=result.stderr,
                )

        if subpath in (".", ""):
            # 整个仓库：直接浅克隆
            _run(["git", *extra, "clone", "--depth=1", "--single-branch", "--branch", ref, url, str(cache_dir)])
        else:
            # 子目录：稀疏克隆
            _run(["git", *extra, "clone", "--depth=1", "--filter=blob:none", "--sparse", url, str(cache_dir)])
            _run(["git", "-C", str(cache_dir), "sparse-checkout", "set", subpath])

    @staticmethod
    def _format_git_err(e: Exception) -> str:
        """从 git 异常里抽出 stderr/returncode 拼成一行可读消息"""
        if isinstance(e, subprocess.CalledProcessError):
            stderr = (e.stderr or "").strip()
            if stderr:
                # stderr 可能多行，只保留最后几行（关键错误通常在末尾）
                lines = stderr.splitlines()[-5:]
                return f"exit={e.returncode} stderr={'; '.join(lines)}"
            return f"exit={e.returncode} (no stderr)"
        return f"{type(e).__name__}: {e}"

    def _robust_move(self, src: str, dst: str, *, retries: int = 3, delay: float = 2.0) -> None:
        """带 Windows 句柄锁重试的目录/文件移动。

        针对 Windows 下杀软实时扫描刚 clone 的文件、或 git 子进程句柄未释放导致的
        PermissionError [WinError 5] / OSError，做有限次重试给外部释放锁的窗口。每次
        重试前清理目标端可能残留的半成品（copytree 中途失败会在 dst 留下不完整目录），
        避免二次 copytree 因 dst 已存在而报错。最终仍失败且源仍在时，清理 dst 残骸后
        上抛，交由调用方回滚 / 清理 cache_tmp。
        """
        last: Optional[Exception] = None
        for attempt in range(retries):
            if attempt > 0 and os.path.exists(dst):
                # 清掉上次可能残留的半成品目标，确保重试干净
                try:
                    if os.path.isdir(dst) and not os.path.islink(dst):
                        _rmtree_readonly(Path(dst))
                    else:
                        os.remove(dst)
                except OSError:
                    pass
            try:
                shutil.move(src, dst)
                return
            except (PermissionError, OSError) as e:
                last = e
                # 仅对「文件被外部进程锁住」类错误重试（WinError 5 = 拒绝访问）；
                # 路径不存在等其它 OSError 直接上抛，避免无效重试
                is_locked = isinstance(e, PermissionError) or (
                    isinstance(e, OSError) and getattr(e, "winerror", None) == 5
                )
                if is_locked and attempt < retries - 1:
                    logger.warning(f"[Installer] move 被外部锁阻塞（{e}），{delay}s 后重试 ({attempt + 1}/{retries})")
                    time.sleep(delay)
                    continue
                # 最终失败：源仍在说明 dst 是 copytree 残骸，清理之
                if os.path.exists(src) and os.path.exists(dst):
                    try:
                        if os.path.isdir(dst) and not os.path.islink(dst):
                            _rmtree_readonly(Path(dst))
                    except OSError:
                        pass
                raise
        if last is not None:
            raise last

    def _suppress_backend_watcher(self, duration: float = 180.0) -> None:
        """安装期间抑制 backend 插件热重载 watcher，避免半安装插件被提前 import 报错。"""
        try:
            from app.core.backend import ChatBackend

            ChatBackend._suppress_watcher_until = time.time() + duration
        except Exception as e:
            logger.debug(f"[Installer] 无法抑制 backend watcher（不影响安装）: {e}")

    def _resume_backend_watcher(self, reload: bool = False, plugin_name: Optional[str] = None) -> None:
        """恢复 backend watcher；安装/启停成功时精准重载目标插件（不触发全量）

        旧实现 reload=True 时调 reload_plugin_subsystems() 全量重载——卸载/安装
        一个插件会把全部插件的 hooks 注销重注册、全部 agents 重载（数十个插件
        联动抖动）。现改为 reload_plugin_targeted(plugin_name) 只处理目标插件。
        """
        try:
            from app.core.backend import ChatBackend

            ChatBackend._suppress_watcher_until = 0.0
        except Exception:
            return
        if not reload:
            return
        try:
            inst = next(iter(ChatBackend._active_instances), None)
            if inst is None:
                return
            # 尽量在主线程执行重载（与 backend watcher 的 _hot_reload_requested 信号同效）；
            # 失败则直接调用，异常被吞掉不影响安装结果
            try:
                from PyQt5.QtCore import QTimer

                QTimer.singleShot(0, lambda: inst.reload_plugin_targeted(plugin_name))
            except Exception:
                inst.reload_plugin_targeted(plugin_name)
        except Exception as e:
            logger.warning(f"[Installer] 触发插件重载失败（不影响安装）: {e}")

    @staticmethod
    def _toggle_git_suffix(url: str) -> str:
        """URL 带 .git 末尾则去掉，不带则补上（仅作用于 path，保留 query/fragment）"""
        if not url:
            return url
        suffix = ""
        path = url
        for sep in ("?", "#"):
            if sep in path:
                idx = path.find(sep)
                suffix = path[idx:]
                path = path[:idx]
                break
        if path.endswith(".git"):
            return path[:-4] + suffix
        return path + ".git" + suffix

    def _build_clone_candidates(self, orig_url: str, proxy_url: str, proxy_extra: list, proxy) -> list:
        """构造按优先级排列的 clone 候选列表（URL, extra_args）。

        顺序：
        1. 用户配置的代理 URL（git_clone_args 已自动补 .git）
        2. 代理 URL 但去掉 .git（部分加速站对无 .git 友好，反向试一次）
        3. 裸直连 + .git（最后保底，国内通常失败）
        4. 裸直连原 URL（兜底）
        """
        candidates: list = [(proxy_url, list(proxy_extra))]
        toggled = self._toggle_git_suffix(proxy_url)
        if toggled != proxy_url:
            candidates.append((toggled, list(proxy_extra)))
        direct_with_git = self._toggle_git_suffix(orig_url)
        if orig_url != direct_with_git:
            candidates.append((direct_with_git, []))
        candidates.append((orig_url, []))
        # 去重保序
        seen, out = set(), []
        for u, e in candidates:
            key = (u, tuple(e))
            if key in seen:
                continue
            seen.add(key)
            out.append((u, e))
        return out

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

            # runtime 组件 loader 模块（model_adapters / loop_policies / storages /
            # serializers / gateways）：命名规则 drifox_rt_{comp}_{plugin}_{stem}，
            # 用原始插件名（可能含 -），与 ui_plugin 不同，必须单独清理。
            # 不清理会导致第三方依赖（如 gateway 平台的 SDK）残留在 sys.modules，
            # Windows 上还可能因 .pyc 句柄占用导致 rmtree 失败（WinError 32）。
            _RT_COMPS = (
                "model_adapters",
                "loop_policies",
                "storages",
                "serializers",
                "gateways",
            )
            for comp in _RT_COMPS:
                for token in (name, safe):
                    head = f"drifox_rt_{comp}_{token}"
                    for mod_name in list(sys.modules.keys()):
                        if mod_name == head or mod_name.startswith(head + "_") or mod_name.startswith(head + "."):
                            del sys.modules[mod_name]

            # 兜底：按 __file__ 落在插件目录内清理（含 deps/ 顶层模块）。
            # gateway 等插件把 SDK vendor 到 deps/ 并 sys.path.insert 后以顶层名
            # import（如 lark_oapi），上述前缀匹配清不掉；模块常驻 sys.modules
            # 会持有 .pyd/.pyc 句柄，Windows 上 rmtree 失败 → 卸载残留 deps 目录。
            for base in (_drifox_dir() / "plugins", _drifox_dir() / "plugins-disabled"):
                plugin_dir = base / name
                if not plugin_dir.is_dir():
                    continue
                try:
                    plugin_dir_str = str(plugin_dir.resolve()).lower()
                except OSError:
                    continue
                for mod_name, mod in list(sys.modules.items()):
                    mod_file = getattr(mod, "__file__", None)
                    if not mod_file:
                        continue
                    try:
                        if plugin_dir_str in str(Path(mod_file).resolve()).lower():
                            del sys.modules[mod_name]
                    except (OSError, ValueError):
                        continue
            importlib.invalidate_caches()
            gc.collect()
        except Exception as e:
            logger.debug(f"[Installer] 清理 sys.modules 失败（忽略）: {e}")

        # 兜底：删除插件可能的 __pycache__ 残留（句柄释放后再删一般能成功）
        for base in (_drifox_dir() / "plugins", _drifox_dir() / "plugins-disabled"):
            pc = base / name / "ui" / "__pycache__"
            if pc.exists():
                _rmtree_readonly(pc)

    # ── 卸载残留兜底（pending-delete） ────────────────────
    # Windows 上被进程加载的 .pyd/.dll（gateway 插件 deps 的 Crypto 等）无法
    # 在运行中删除：rmtree 删掉其余文件后该文件残留。记录到清单，进程重启后
    # 句柄释放，下次访问 installer 时自动清理。

    _PENDING_DELETE_FILE = ".pending-delete.json"

    def _pending_delete_path(self) -> Path:
        return self._plugins_dir / self._PENDING_DELETE_FILE

    def _load_pending_delete(self) -> dict:
        """读取残留清单 {path_str: timestamp}"""
        try:
            p = self._pending_delete_path()
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug(f"[Installer] 读取残留清单失败: {e}")
        return {}

    def _record_pending_delete(self, paths) -> None:
        """记录卸载残留目录（进程重启后清理）"""
        try:
            self._plugins_dir.mkdir(parents=True, exist_ok=True)
            existing = self._load_pending_delete()
            existing.update({str(p): time.time() for p in paths})
            self._pending_delete_path().write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[Installer] 记录残留目录失败: {e}")

    def _cleanup_pending_delete(self) -> int:
        """清理上次卸载残留（进程重启后 DLL/.pyd 句柄已释放）

        Returns:
            成功清理的残留目录数
        """
        pending = self._load_pending_delete()
        if not pending:
            return 0
        cleaned = 0
        remaining: dict = {}
        for path_str, ts in pending.items():
            p = Path(path_str)
            if not p.exists():
                cleaned += 1
                continue
            if p.is_file() or p.is_symlink():
                # rmtree 只接受目录；清单可能含文件级残留（rmtree 报告的失败文件）
                try:
                    p.unlink()
                    cleaned += 1
                except OSError:
                    remaining[path_str] = ts
            else:
                if _rmtree_readonly(p):
                    cleaned += 1
                else:
                    remaining[path_str] = ts
        try:
            if remaining:
                self._pending_delete_path().write_text(
                    json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            else:
                self._pending_delete_path().unlink(missing_ok=True)
        except Exception:
            pass
        # 隔离目录（上次卸载时被锁 .pyd/.dll rename 到此；重启后句柄已释放）一并清掉
        try:
            if self._quarantine_dir.exists():
                _rmtree_readonly(self._quarantine_dir)
        except Exception:
            pass
        if cleaned:
            logger.info(f"[Installer] 启动清理 {cleaned} 个卸载残留目录")
        return cleaned

    # ── 卸载 ─────────────────────────────────────────────

    def remove(self, name: str) -> bool:
        """删除插件目录（更新/卸载共用）

        Args:
            name: 插件名

        Returns:
            True 删除成功或目录不存在
        """
        # 0. 停止并摘除该插件注册的 gateway 平台 adapter：adapter 依赖的 SDK
        #    （如 lark_oapi → Crypto）被进程加载后 .pyd/.dll 句柄占用，不摘除
        #    会导致 rmtree 删不干净（残留 deps 目录）。stop_plugin_platforms
        #    内部先调度 stop（释放 WS 连接线程）再 pop 摘除实例引用；
        #    wait=True 确保 stop 完成后再删，最大限度避免 .pyd 占用。
        self._stop_gateway_platforms(name, wait=True)
        # 清理模块缓存（UI 注册表卸载由 GUI 线程在主线程先行完成）
        self._purge_plugin_module_cache(name)
        removed = False
        leftover: list = []
        for base in (self._plugins_dir, self._disabled_dir):
            target = base / name
            if target.exists():
                # _rmtree_relocate：先正常删，删不掉的被锁 .pyd/.dll 同卷改名移出
                # 插件目录（让空），再二次 rmtree 清空；仅当文件被其它进程永久独占、
                # 连 rename 都不行时才残留到 leftover（记录待重启清理）。
                if _rmtree_relocate(self, target, leftover):
                    removed = True
                    logger.info(f"[Installer] Removed plugin {name}")
        if leftover:
            # 部分文件被其它进程永久独占、连 rename 都失败 → 记录待下次启动/重启清理。
            # 占用文件已被尽可能移出，目录通常已让空，市场侧不再显示为插件，
            # 用户视角卸载已完成；残留仅占磁盘，重启后自动清掉。
            self._record_pending_delete(leftover)
            logger.warning(
                f"[Installer] 插件 {name} 部分文件残留（可能被其它进程占用），已记录待下次启动清理: {leftover[:5]}"
            )
        if removed:
            self.invalidate_installed_cache()
        return True

    @staticmethod
    def _stop_gateway_platforms(name: str, wait: bool = False) -> None:
        """卸载/清理前停止插件注册的 gateway 平台（摘除 adapter 引用 + 停止连接）

        Args:
            name: 插件名
            wait: True 时阻塞等待 adapter.stop() 完成（释放 SDK/WS 线程引用，
                避免随后 rmtree 时 deps 的 .pyd/.dll 仍被占用）
        """
        try:
            from app.gateway.manager import get_platform_manager

            pm = get_platform_manager()
            if pm is not None:
                pm.stop_plugin_platforms(name, wait=wait)
        except Exception as e:
            logger.warning(f"[Installer] 停止 gateway 平台({name})失败: {e}")
        # registry 中的 def 持有 adapter_factory（插件模块函数引用），不摘除
        # 则模块链（→ SDK → Crypto）无法 GC，deps 的 .pyd 句柄不释放。
        try:
            from app.plugins.registries.gateway_platform_registry import (
                GatewayPlatformRegistry,
            )

            GatewayPlatformRegistry.get_instance().unregister_source(f"plugin:{name}")
        except Exception as e:
            logger.debug(f"[Installer] unregister gateway source({name})失败（忽略）: {e}")

    def uninstall(self, name: str) -> bool:
        """卸载插件（删除本地目录）"""
        if not self.remove(name):
            return False
        logger.info(f"[Installer] Uninstalled plugin {name}")
        return True

    # ── 启用 / 禁用 ──────────────────────────────────────

    def disable(self, name: str) -> bool:
        """禁用已启用的插件

        - 用户插件（.drifox/plugins/）：移动到 plugins-disabled 目录
        - 内置非 system 插件（项目根 plugins/ 且 manifest type != system）：
          通过 PluginManager 写 Settings 禁用（不移动文件，目录随主程序分发）
        真系统插件（manifest type == system）不可禁用。
        """
        src = self._plugins_dir / name
        if src.exists():
            # 用户插件：move 到 plugins-disabled
            # 清理模块缓存（UI 注册表卸载由主线程先行完成，此处不碰 GUI）
            self._purge_plugin_module_cache(name)
            # 抑制 backend watcher：避免移动期间 watcher 抢跑 import 插件的 deps
            # （如 gateway 插件的 Crypto 等 .pyd），造成 WinError 5 拒绝访问
            self._suppress_backend_watcher()
            try:
                self._disabled_dir.mkdir(parents=True, exist_ok=True)
                self._robust_move(str(src), str(self._disabled_dir / name))
                self.invalidate_installed_cache()
                logger.info(f"[Installer] Disabled plugin {name}")
                # 恢复 watcher 并精准重载目标插件（仅清理该插件，不触发全量）
                self._resume_backend_watcher(reload=True, plugin_name=name)
                return True
            except Exception as e:
                logger.error(f"[Installer] Disable {name} failed: {e}")
                # 恢复 watcher（不重载），避免抑制窗口残留
                self._resume_backend_watcher(reload=False)
                return False
        # 内置非 system 插件（项目根 plugins/ 且 type != system）→ 配置禁用
        system_dir = getattr(self, "_system_dir", None)
        if system_dir is not None and (system_dir / name).is_dir():
            manifest = self._read_manifest_at(system_dir / name)
            if manifest is None or manifest.get("type") != "system":
                return self._set_managed_enabled(name, enabled=False)
        return False

    def enable(self, name: str) -> bool:
        """启用已禁用的插件

        - 用户插件（plugins-disabled/）：移回 plugins/
        - 内置非 system 插件（项目根 plugins/ 且 type != system）：
          通过 PluginManager 写 Settings 启用
        """
        src = self._disabled_dir / name
        if src.exists():
            # 释放 deps 模块句柄（gateway 插件 vendor 的 Crypto 等 .pyd 被进程
            # 加载后会占用文件，移动前清理可降低 WinError 5 概率）
            self._purge_plugin_module_cache(name)
            # 抑制 backend watcher：避免移动期间 watcher 抢跑 import 插件的 deps
            # （如 gateway 插件的 Crypto 等 .pyd），造成 WinError 5 拒绝访问
            self._suppress_backend_watcher()
            try:
                self._plugins_dir.mkdir(parents=True, exist_ok=True)
                self._robust_move(str(src), str(self._plugins_dir / name))
                self.invalidate_installed_cache()
                logger.info(f"[Installer] Enabled plugin {name}")
                # 恢复 watcher 并精准重载目标插件（仅加载该插件，不触发全量）
                self._resume_backend_watcher(reload=True, plugin_name=name)
                return True
            except Exception as e:
                logger.error(f"[Installer] Enable {name} failed: {e}")
                # 恢复 watcher（不重载），避免抑制窗口残留
                self._resume_backend_watcher(reload=False)
                return False
        # 内置非 system 插件 → 配置启用
        system_dir = getattr(self, "_system_dir", None)
        if system_dir is not None and (system_dir / name).is_dir():
            manifest = self._read_manifest_at(system_dir / name)
            if manifest is None or manifest.get("type") != "system":
                return self._set_managed_enabled(name, enabled=True)
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
