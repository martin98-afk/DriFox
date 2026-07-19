# -*- coding: utf-8 -*-
"""插件安装器 — 通过 git 拉取市场插件

设计要点：
- 临时下载到 cache 目录，避免触发 watchfiles 插件热更新
- 下载完成后再原子式 move 到 plugins 目录
- 支持版本检测与更新
- 支持多种 source 类型（兼容 Claude Code marketplace 格式）
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
        self._cache_dir = drifox / "cache" / "install_tmp"

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

        return self._install_by_source(name, source, target, plugin_meta.get("_marketplace_source"))

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

    def _install_relative(self, name: str, relative_path: str, target: Path,
                          marketplace_source: dict = None) -> bool:
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
                check=True, capture_output=True, text=True, **kwargs,
            )
        else:
            # 子目录：稀疏克隆
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
