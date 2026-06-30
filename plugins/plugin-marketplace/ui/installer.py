# -*- coding: utf-8 -*-
"""插件安装器 — 通过 git 拉取市场插件

设计要点：
- 临时下载到 cache 目录，避免触发 watchfiles 插件热更新
- 下载完成后再原子式 move 到 plugins 目录
"""

import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from loguru import logger


class PluginInstaller:
    """插件安装器

    支持 git-subdir 类型的 source（仅克隆子目录到本地）
    """

    def __init__(self):
        self._plugins_dir = Path.home() / '.drifox' / "plugins"
        self._cache_dir = Path.home() / '.drifox' / "cache" / "install_tmp"

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
            logger.info(f"[Installer] Plugin {name} already exists")
            return True

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
                # 清理失败的 cache 目录
                shutil.rmtree(cache_tmp, ignore_errors=True)
                raise

            # === 2. 从 cache 移到 plugins 目录（只移动子目录内容）===
            sub_src = cache_tmp / subpath
            if not sub_src.exists():
                shutil.rmtree(cache_tmp, ignore_errors=True)
                raise RuntimeError(f"Subpath {subpath} not found in cache")

            # target 可能中间含有子路径，用 rename 或 move
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sub_src), str(target))

            # === 3. 清理 cache ===
            shutil.rmtree(cache_tmp, ignore_errors=True)

            logger.info(f"[Installer] Installed plugin {name} -> {target}")
            return True

        except Exception as e:
            logger.error(f"[Installer] Install {name} failed: {e}")
            return False

    def _sparse_clone(self, url: str, subpath: str, ref: str, cache_dir: Path):
        """克隆仓库指定子目录到 cache_dir"""
        subprocess.run(
            ["git", "clone", "--depth=1", "--filter=blob:none", "--sparse", url, str(cache_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(cache_dir), "sparse-checkout", "set", subpath],
            check=True,
            capture_output=True,
            text=True,
        )

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

    def is_installed(self, name: str) -> bool:
        return (self._plugins_dir / name).exists()


_instance: Optional[PluginInstaller] = None


def get_installer() -> PluginInstaller:
    global _instance
    if _instance is None:
        _instance = PluginInstaller()
    return _instance
