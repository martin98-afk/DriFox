# -*- coding: utf-8 -*-
"""数据层 — 缓存目录扫描 + 大小计算 + 异步 Workers

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作通过 os/shutil/stdlib 完成
"""

import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger


# ── 路径常量 ──────────────────────────────────────────────


def _drifox_dir() -> Path:
    """获取应用数据目录（与 app.utils.utils.get_app_data_dir 保持一致）

    开发环境: 当前目录/.drifox
    PyInstaller打包: ~/.drifox（用户 home 目录，可写）
    macOS .app: ~/Library/Application Support/Drifox/.drifox
    """
    if not hasattr(sys, '_MEIPASS') and not getattr(sys, 'frozen', False):
        return Path('.drifox')
    if sys.platform == 'darwin':
        try:
            from AppKit import NSApplicationSupportDirectory, NSFileManager, NSUserDomainMask
            paths = NSFileManager.defaultManager().URLsForDirectory_inDomains_(
                NSApplicationSupportDirectory, NSUserDomainMask
            )
            if paths:
                app_support_path = paths[0].fileSystemRepresentation().decode('utf-8')
                app_support = Path(app_support_path) / 'Drifox'
                app_support.mkdir(parents=True, exist_ok=True)
                return app_support / '.drifox'
        except Exception:
            pass
    return Path.home() / '.drifox'


# ── 缓存类型定义 ──────────────────────────────────────────

CACHE_DEFS: List[Tuple[str, str, str, str, bool]] = [
    ("backups", "📁", "备份文件", "backups", True),
    ("logs", "📝", "日志文件", "logs", False),
    ("cache", "🗃️", "应用缓存", "cache", True),
    ("screenshots", "📸", "截图文件", "screenshots", False),
    ("archived", "📦", "归档会话", "archived", False),
    ("share_sessions", "📤", "分享会话文件", "share/sessions", False),
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
                    except (OSError, PermissionError):
                        pass
    except (OSError, PermissionError):
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
                except (OSError, PermissionError):
                    pass
    except (OSError, PermissionError):
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
                    except (OSError, PermissionError):
                        pass
    except (OSError, PermissionError):
        pass


# ── 内存释放 ──────────────────────────────────────────


def _release_memory():
    """
    深度释放进程内存，归还给操作系统。

    链路：
    1. 清理 DriFox 全局 LRU 缓存（HTML 渲染/token 估算/grep 编译等）
    2. 清理 Qt QPixmapCache
    3. 清理 importlib 缓存
    4. 清理 re 正则编译缓存 + linecache 行缓存
    5. gc.collect(2) 全代回收
    6. 堆压缩 + 激进工作集归还 OS（Windows: SetProcessWorkingSetSize / Linux: malloc_trim）

    Returns:
        (before_rss, after_rss, collected_objects)
    """
    import ctypes
    import gc
    import sys as _sys

    before = _get_process_memory()

    # ── 1. DriFox 内部 LRU 缓存 ──
    try:
        from app.main_widget import _cleanup_global_lru_caches

        _cleanup_global_lru_caches()
    except Exception:
        pass

    # ── 2. Qt 像素图缓存 ──
    try:
        from PyQt5.QtGui import QPixmapCache

        QPixmapCache.clear()
    except Exception:
        pass

    # ── 3. importlib 缓存（模块查找缓存） ──
    try:
        import importlib

        importlib.invalidate_caches()
    except Exception:
        pass

    # ── 4. re 正则 + linecache 行缓存 ──
    try:
        import re

        re.purge()
    except Exception:
        pass
    try:
        import linecache

        linecache.clearcache()
    except Exception:
        pass

    # ── 5. Python GC 全代回收 ──
    # gc.collect() 默认只收第 0 代；gc.collect(2) 收全部三代
    collected = gc.collect(2)

    # ── 6. 堆压缩 + 归还 OS ──
    try:
        if _sys.platform == "win32":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # 6a. 压缩堆（消除碎片，合并空闲块）
            heap = kernel32.GetProcessHeap()
            if heap:
                kernel32.HeapCompact(heap, 0)
            # 6b. 激进工作集修剪：SetProcessWorkingSetSize(-1, -1)
            #     移除进程最小工作集限制，让 Windows 积极换出未使用页面
            #     比 EmptyWorkingSet 更彻底——不再保留任何"锁住"的页面
            h = kernel32.GetCurrentProcess()
            kernel32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1).value, ctypes.c_size_t(-1).value)
        elif _sys.platform == "linux":
            libc = ctypes.CDLL("libc.so.6", use_last_error=True)
            libc.malloc_trim(0)
    except Exception:
        pass

    after = _get_process_memory()
    return before, after, collected


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
