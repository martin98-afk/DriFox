# -*- coding: utf-8 -*-
"""插件安装器 — 通过 git 拉取市场插件"""
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from loguru import logger

from app.utils.utils import get_app_data_dir


class PluginInstaller:
    """插件安装器

    支持 git-subdir 类型的 source（仅克隆子目录到本地）
    """

    def __init__(self):
        self._plugins_dir = get_app_data_dir() / "plugins"

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

        try:
            # 使用 sparse-checkout 克隆子目录
            url = source.get("url", "")
            subpath = source.get("path", "")
            ref = source.get("ref", "main")
            if not url or not subpath:
                return False

            target.parent.mkdir(parents=True, exist_ok=True)
            self._sparse_clone(url, subpath, ref, target)
            logger.info(f"[Installer] Installed plugin {name} -> {target}")
            return True
        except Exception as e:
            logger.error(f"[Installer] Install {name} failed: {e}")
            return False

    def _sparse_clone(self, url: str, subpath: str, ref: str, target: Path):
        """克隆仓库的指定子目录"""
        # 临时目录用于初始化 sparse-checkout
        tmp = target.parent / f".{target.name}_tmp"
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", "--filter=blob:none",
                 "--sparse", url, str(tmp)],
                check=True, capture_output=True, text=True
            )
            subprocess.run(
                ["git", "-C", str(tmp), "sparse-checkout", "set", subpath],
                check=True, capture_output=True, text=True
            )
            # 移动子目录到目标位置
            sub_src = tmp / subpath
            if not sub_src.exists():
                raise RuntimeError(f"Subpath {subpath} not found in repo")
            shutil.move(str(sub_src), str(target))
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

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
