# -*- coding: utf-8 -*-
"""插件安装器 — 通过 git 拉取市场插件

设计要点：
- 临时下载到 cache 目录，避免触发 watchfiles 插件热更新
- 下载完成后再原子式 move 到 plugins 目录
- 支持版本检测与更新
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from .data import compare_versions


# ── 环境检测 ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEV_DRIFOX = _PROJECT_ROOT / ".drifox"
_USER_DRIFOX = Path.home() / ".drifox"


def _drifox_dir() -> Path:
    """查找 .drifox 目录（开发环境优先，兜底用户目录）"""
    if _DEV_DRIFOX.exists():
        return _DEV_DRIFOX
    return _USER_DRIFOX


class PluginInstaller:
    """插件安装器

    支持 git-subdir 类型的 source（仅克隆子目录到本地）
    支持版本检测与增量更新
    """

    def __init__(self):
        drifox = _drifox_dir()
        self._plugins_dir = drifox / "plugins"
        self._cache_dir = drifox / "cache" / "install_tmp"

    # ── 安装 ─────────────────────────────────────────────

    def install(self, plugin_meta: dict) -> bool:
        """安装插件

        Args:
            plugin_meta: marketplace.json 中的插件元数据

        Returns:
            True 安装成功
        """
        source = plugin_meta.get("source", {})
        if source.get("type") != "git-subdir":
            logger.error(f"[Installer] 不支持的 source 类型: {source.get('type')}")
            return False

        name = plugin_meta.get("name", "")
        if not name:
            return False

        target = self._plugins_dir / name
        if target.exists():
            logger.info(f"[Installer] Plugin {name} already exists, skipping install")
            return True

        return self._download_and_move(name, source, target)

    def update(self, plugin_meta: dict) -> bool:
        """更新插件 — 删除旧版后重新下载

        Args:
            plugin_meta: marketplace.json 中的插件元数据

        Returns:
            True 更新成功
        """
        name = plugin_meta.get("name", "")
        if not name:
            return False

        source = plugin_meta.get("source", {})
        if source.get("type") != "git-subdir":
            logger.error(f"[Installer] 不支持的 source 类型: {source.get('type')}")
            return False

        target = self._plugins_dir / name
        remote_ver = plugin_meta.get("version", "0.0.0")

        # 先删除旧版目录
        if target.exists():
            try:
                shutil.rmtree(target)
                logger.info(f"[Installer] Removed old version of {name} before update")
            except Exception as e:
                logger.error(f"[Installer] Failed to remove old {name}: {e}")
                return False

        # 重新下载安装
        success = self._download_and_move(name, source, target)
        if success:
            logger.info(f"[Installer] Updated plugin {name} -> v{remote_ver}")
        return success

    def _download_and_move(self, name: str, source: dict, target: Path) -> bool:
        """从 git 源下载插件并移动到目标目录（核心逻辑）"""
        url = source.get("url", "")
        subpath = source.get("path", "")
        ref = source.get("ref", "main")
        if not url or not subpath:
            return False

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

            # === 2. 从 cache 移到 plugins 目录（只移动子目录内容）===
            sub_src = cache_tmp / subpath
            if not sub_src.exists():
                shutil.rmtree(cache_tmp, ignore_errors=True)
                raise RuntimeError(f"Subpath {subpath} not found in cache")

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sub_src), str(target))

            # === 3. 清理 cache ===
            shutil.rmtree(cache_tmp, ignore_errors=True)

            logger.info(f"[Installer] Installed plugin {name} -> {target}")
            return True

        except Exception as e:
            logger.error(f"[Installer] Download {name} failed: {e}")
            return False

    def _sparse_clone(self, url: str, subpath: str, ref: str, cache_dir: Path):
        """克隆仓库指定子目录到 cache_dir"""
        # Windows 上避免 git 弹出控制台黑框
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        subprocess.run(
            ["git", "clone", "--depth=1", "--filter=blob:none", "--sparse", url, str(cache_dir)],
            check=True, capture_output=True, text=True, **kwargs,
        )
        subprocess.run(
            ["git", "-C", str(cache_dir), "sparse-checkout", "set", subpath],
            check=True, capture_output=True, text=True, **kwargs,
        )

    # ── 卸载 ─────────────────────────────────────────────

    def uninstall(self, name: str) -> bool:
        """卸载插件（删除本地目录）"""
        target = self._plugins_dir / name
        if not target.exists():
            return False
        try:
            shutil.rmtree(target)
            logger.info(f"[Installer] Uninstalled plugin {name}")
            return True
        except Exception as e:
            logger.error(f"[Installer] Uninstall {name} failed: {e}")
            return False

    # ── 状态查询 ─────────────────────────────────────────

    def is_installed(self, name: str) -> bool:
        """检查插件是否已安装"""
        return (self._plugins_dir / name).exists()

    def get_installed_version(self, name: str) -> Optional[str]:
        """读取已安装插件的本地版本号

        Args:
            name: 插件名称

        Returns:
            版本号字符串，如 "1.0.0"，如果未安装或读取失败返回 None
        """
        target = self._plugins_dir / name
        if not target.exists():
            return None

        manifest_path = target / ".drifox-plugin" / "plugin.json"
        if not manifest_path.exists():
            manifest_path = target / ".claude-plugin" / "plugin.json"
        if not manifest_path.exists():
            return None

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return manifest.get("version")
        except Exception as e:
            logger.warning(f"[Installer] Failed to read version for {name}: {e}")
            return None

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
            return (False, None, None)  # 未安装，不触发更新

        remote_ver = plugin_meta.get("version")
        if not remote_ver:
            return (False, local_ver, None)  # 远端无版本信息

        has_update = compare_versions(local_ver, remote_ver) < 0
        return (has_update, local_ver, remote_ver)


_instance: Optional[PluginInstaller] = None


def get_installer() -> PluginInstaller:
    global _instance
    if _instance is None:
        _instance = PluginInstaller()
    return _instance
