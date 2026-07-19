# -*- coding: utf-8 -*-
"""数据层 — 缓存目录扫描 + 大小计算 + 异步 Workers

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作通过 os/shutil/stdlib 完成
"""

import os
import shutil
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger


# ── 路径常量 ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEV_DRIFOX = _PROJECT_ROOT / ".drifox"
_USER_DRIFOX = Path.home() / ".drifox"


def _drifox_dir() -> Optional[Path]:
    """查找 .drifox 目录（开发环境 → 用户目录兜底）"""
    if _DEV_DRIFOX.exists():
        return _DEV_DRIFOX
    if _USER_DRIFOX.exists():
        return _USER_DRIFOX
    return None


# ── 缓存类型定义 ──────────────────────────────────────────

CACHE_DEFS: List[Tuple[str, str, str, str, bool]] = [
    ("backups", "📁", "备份文件", "backups", True),
    ("logs", "📝", "日志文件", "logs", False),
    ("cache", "🗃️", "应用缓存", "cache", True),
    ("screenshots", "📸", "截图文件", "screenshots", False),
    ("archived", "📦", "归档会话", "archived", False),
]


# ── 实用函数 ──────────────────────────────────────────────


def _format_size(n: int) -> str:
    """格式化字节大小，如 1234567 → '1.2 MB'"""
    if n < 0:
        return "N/A"
    if n >= 1073741824:
        return f"{n / 1073741824:.1f} GB"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _calc_dir_size(path: Path, dir_mode: bool) -> int:
    """计算目录大小"""
    if not path.exists():
        return 0
    total = 0
    try:
        if dir_mode:
            for entry in path.iterdir():
                if entry.is_dir():
                    total += _walk_dir_size(entry)
        else:
            for entry in path.iterdir():
                if entry.is_file():
                    try:
                        total += entry.stat().st_size
                    except OSError, PermissionError:
                        pass
    except OSError, PermissionError:
        pass
    return total


def _walk_dir_size(path: Path) -> int:
    """递归计算目录总大小"""
    total = 0
    try:
        for root, _dirs, files in os.walk(str(path)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError, PermissionError:
                    pass
    except OSError, PermissionError:
        pass
    return total


def _get_process_memory() -> Optional[int]:
    """获取当前进程 RSS 内存（字节）"""
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss
    except Exception:
        return None


def _delete_cache(path: Path, dir_mode: bool):
    """删除缓存目录中的内容"""
    if not path.exists():
        return
    try:
        if dir_mode:
            for entry in path.iterdir():
                if entry.is_dir():
                    shutil.rmtree(str(entry), ignore_errors=True)
        else:
            for entry in path.iterdir():
                if entry.is_file():
                    try:
                        entry.unlink()
                    except OSError, PermissionError:
                        pass
    except OSError, PermissionError:
        pass


# ── 扫描工作器 ──────────────────────────────────────────


class _ScanWorker(QObject):
    """后台扫描所有缓存目录大小"""

    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, drifox_dir: Path):
        super().__init__()
        self._drifox_dir = drifox_dir

    def run(self):
        try:
            sizes: Dict[str, int] = {}
            for cid, _icon, _label, rel_path, dir_mode in CACHE_DEFS:
                full = self._drifox_dir / rel_path
                sizes[cid] = _calc_dir_size(full, dir_mode)
            self.finished.emit(sizes)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ── 清理工作器 ──────────────────────────────────────────


class _CleanWorker(QObject):
    """后台执行文件删除"""

    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, drifox_dir: Path, targets: List[Tuple[str, str, str, str, bool]]):
        super().__init__()
        self._drifox_dir = drifox_dir
        self._targets = targets

    def run(self):
        try:
            freed: Dict[str, int] = {}
            for cid, _icon, label, rel_path, dir_mode in self._targets:
                self.progress.emit(label)
                full = self._drifox_dir / rel_path
                before = _calc_dir_size(full, dir_mode)
                _delete_cache(full, dir_mode)
                after = _calc_dir_size(full, dir_mode)
                freed[cid] = before - after
            self.finished.emit(freed)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")
