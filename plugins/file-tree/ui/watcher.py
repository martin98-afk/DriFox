# -*- coding: utf-8 -*-
"""目录变更监听器 — 封装 QFileSystemWatcher 带防抖

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
"""

import os
from typing import Dict

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from loguru import logger


_MAX_WATCH_PATHS = 50
_WATCH_DEBOUNCE_MS = 500


class _DirWatcher(QObject):
    """封装 QFileSystemWatcher，管理已展开目录的监听

    特性：
    - 自动限制最大监听路径数
    - 变更通知防抖（避免短时间内重复刷新）
    - 路径增删接口
    """

    dir_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt5.QtCore import QFileSystemWatcher

        self._watcher = QFileSystemWatcher(self)
        self._debounce_timers: Dict[str, QTimer] = {}
        self._watcher.directoryChanged.connect(self._on_dir_changed)

    def add_path(self, path: str):
        if not os.path.isdir(path):
            return
        if path in self._watcher.directories():
            return
        if len(self._watcher.directories()) >= _MAX_WATCH_PATHS:
            oldest = self._watcher.directories()[0]
            self._watcher.removePath(oldest)
            logger.debug(f"[DirWatcher] 超出监听上限，移除: {oldest}")

        self._watcher.addPath(path)
        logger.debug(f"[DirWatcher] 开始监听: {path}")

    def remove_path(self, path: str):
        if path in self._watcher.directories():
            self._watcher.removePath(path)
            logger.debug(f"[DirWatcher] 停止监听: {path}")

        if path in self._debounce_timers:
            self._debounce_timers[path].stop()
            self._debounce_timers[path].deleteLater()
            del self._debounce_timers[path]

    def clear(self):
        paths = list(self._watcher.directories())
        for p in paths:
            self._watcher.removePath(p)
        for timer in self._debounce_timers.values():
            timer.stop()
            timer.deleteLater()
        self._debounce_timers.clear()
        logger.debug("[DirWatcher] 清空所有监听")

    def _on_dir_changed(self, path: str):
        if path in self._debounce_timers:
            self._debounce_timers[path].stop()
        else:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda p=path: self._emit_change(p))
            self._debounce_timers[path] = timer

        self._debounce_timers[path].start(_WATCH_DEBOUNCE_MS)

    def _emit_change(self, path: str):
        if path in self._debounce_timers:
            self._debounce_timers[path].deleteLater()
            del self._debounce_timers[path]
        self.dir_changed.emit(path)
