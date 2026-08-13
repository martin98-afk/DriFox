# -*- coding: utf-8 -*-
"""project-dashboard 异步采集 — QThread worker + 模块级缓存 + 欢迎卡片重渲染

render_func 主线程同步调用，git/文件采集耗时（数百 ms）必须后台执行：
- 模块级单例 worker，避免重复启动
- 采集完成 → 缓存写入 + 触发欢迎卡片重渲染（set_welcome_mode 同 mode）
- 缓存 key = (git_root, HEAD, 当天日期)：HEAD 变化自动失效重采
"""

from datetime import datetime
from typing import Optional

from PyQt5.QtCore import QObject, QThread, pyqtSignal
from loguru import logger

# 延迟导入（避免模块加载时依赖 sys.path 已含 ui 目录）
_collector = None


class _CollectWorker(QObject):
    """后台采集 worker"""

    finished = pyqtSignal(object)  # dict
    error = pyqtSignal(str)

    def __init__(self, project_root: str, is_dark: bool):
        super().__init__()
        self._root = project_root
        self._is_dark = is_dark

    def run(self):
        try:
            from dashboard import collect_data

            data = collect_data(self._root)
            data["is_dark"] = self._is_dark
            self.finished.emit(data)
        except Exception as e:
            self.error.emit(f"{e}")


class _Collector:
    """异步采集管理器（模块级单例）"""

    def __init__(self):
        self._thread: Optional[QThread] = None
        self._worker: Optional[_CollectWorker] = None
        self._cache: Optional[dict] = None
        self._cache_key: tuple = ()

    def get_cached(self, cache_key: tuple) -> Optional[dict]:
        """缓存命中返回 data，否则 None"""
        if self._cache is not None and self._cache_key == cache_key:
            return self._cache
        return None

    def start(self, project_root: str, is_dark: bool, cache_key: tuple):
        """启动后台采集（幂等：正在跑则跳过）"""
        if self._thread is not None and self._thread.isRunning():
            return
        self._cache_key = cache_key
        self._cleanup()
        w = _CollectWorker(project_root, is_dark)
        t = QThread()
        w.moveToThread(t)
        t.started.connect(w.run)
        w.finished.connect(self._on_done)
        w.error.connect(self._on_error)
        w.finished.connect(t.quit)
        w.error.connect(t.quit)
        w.finished.connect(w.deleteLater)
        w.error.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)
        self._thread, self._worker = t, w
        t.start()

    def _on_done(self, data: dict):
        self._cache = data
        self._thread = None
        self._worker = None
        _refresh_welcome_cards()
        logger.info("[project-dashboard] collect done")

    def _on_error(self, err: str):
        self._thread = None
        self._worker = None
        self._cache = {"error": f"采集失败: {err}"}
        _refresh_welcome_cards()
        logger.error(f"[project-dashboard] collect error: {err}")

    def _cleanup(self):
        """清理残留引用（线程已由信号链 deleteLater）"""
        self._thread = None
        self._worker = None


def _refresh_welcome_cards():
    """触发所有窗口欢迎卡片重渲染（当前显示 project-dashboard tab 的立即刷新）"""
    try:
        from app.core.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        for mw in list(reg._window_main_widgets.values()):
            try:
                wid = getattr(mw, "_window_id", None)
                if wid is None:
                    continue
                cache = getattr(mw, "_welcome_card_cache", {})
                card = cache.get(wid)
                if card is None:
                    continue
                mode = getattr(card, "_welcome_mode", "")
                if mode == "project-dashboard":
                    card.set_welcome_mode("project-dashboard")
            except Exception:
                pass
    except Exception:
        pass


def get_collector() -> _Collector:
    """获取异步采集单例"""
    global _collector
    if _collector is None:
        _collector = _Collector()
    return _collector


def build_cache_key(project_root: str, data: Optional[dict] = None) -> tuple:
    """缓存 key：git 根 + HEAD + 日期（HEAD 变化自动重采）"""
    try:
        from dashboard import _run_git

        head = _run_git(project_root, "rev-parse", "--short", "HEAD") or "none"
    except Exception:
        head = "none"
    return (project_root, head, datetime.now().strftime("%Y-%m-%d"))
