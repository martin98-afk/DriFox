# -*- coding: utf-8 -*-
"""目录扫描 — 异步扫描 + 过滤规则 + 数据结构

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作直接通过 stdlib 完成
"""

import os
import traceback
from typing import List, Optional, Set

from PySide6.QtCore import QObject, Signal


# ── 过滤规则 ──────────────────────────────────────────────

_HIDDEN_DIRS: Set[str] = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    ".env",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
    ".playwright-mcp",
}

_HIDDEN_FILES: Set[str] = {
    ".DS_Store",
    "Thumbs.db",
}


def _should_show(name: str, is_dir: bool) -> bool:
    """智能过滤：判断文件/目录是否应该显示"""
    if is_dir:
        return name not in _HIDDEN_DIRS
    if name in _HIDDEN_FILES:
        return False
    if name.startswith("."):
        return False
    return True


# ── 目录条目数据结构 ────────────────────────────────────


class _DirEntry:
    """目录条目数据结构"""

    __slots__ = ("name", "path", "is_dir", "children")

    def __init__(self, name: str, path: str, is_dir: bool):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.children: Optional[List["_DirEntry"]] = None

    def __repr__(self):
        return f"<{('DIR' if self.is_dir else 'FILE')} {self.name}>"


# ── 异步目录扫描工作器 ─────────────────────────────────


class _TreeScanner(QObject):
    """后台线程目录扫描器

    ★ 目录在构造时绑定、``scan()`` 无参：``QThread.started`` 只能连接到
    bound method（receiver=scanner，亲和 worker 线程）才能让扫描真正落在
    worker 线程执行。此前 connect 到 lambda（无 receiver）会被 PyQt queued
    回主线程，扫描全程占用主线程（卡 UI 元凶之一）。
    """

    finished = Signal(object)  # List[_DirEntry]
    error = Signal(str)

    def __init__(self, directory: str):
        super().__init__()
        self._directory = directory

    def scan(self):
        """扫描构造时绑定的目录（在工作线程中调用）"""
        try:
            directory = self._directory
            entries: List[_DirEntry] = []
            if not os.path.isdir(directory):
                self.finished.emit(entries)
                return

            with os.scandir(directory) as it:
                for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
                    try:
                        name = entry.name
                        is_dir = entry.is_dir()
                        if not _should_show(name, is_dir):
                            continue
                        entries.append(_DirEntry(name=name, path=entry.path, is_dir=is_dir))
                    except OSError:
                        continue

            self.finished.emit(entries)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")
