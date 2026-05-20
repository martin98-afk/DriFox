# -*- coding: utf-8 -*-
"""
Worker 基类 — 统一生命周期管理

统一 cancel() / cleanup() / is_cancelled 等生命周期方法。
所有 Worker（OpenAIChatWorker / AutoLoopWorker / SubAgentExecutor）可继承此类。

注意：不使用 ABC 避免与 QThread 元类冲突。
"""
from abc import ABC
from typing import Any, Optional

from PyQt5.QtCore import QThread
from loguru import logger


class BaseWorker(QThread):
    """Worker 基类

    提供统一的生命周期管理接口。
    子类只需实现 do_work() 方法。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_cancelled = False

    # ============================================================
    # 生命周期
    # ============================================================

    def cancel(self):
        """请求取消当前执行"""
        self._is_cancelled = True
        logger.debug(f"[{self.__class__.__name__}] Cancelled")

    def cleanup(self):
        """清理资源（由调用方在 Worker 完成后调用）"""
        pass

    @property
    def is_cancelled(self) -> bool:
        """当前是否已取消"""
        return self._is_cancelled

    def is_running(self) -> bool:
        """当前是否正在运行"""
        return self.isRunning()

    # ============================================================
    # 子类需实现
    # ============================================================

    def run(self) -> None:
        """入口点，默认调用 do_work()"""
        self._is_cancelled = False
        try:
            self.do_work()
        except Exception as e:
            logger.exception(f"[{self.__class__.__name__}] Unhandled error in do_work: {e}")
            self._on_error(e)
        finally:
            self._on_finished()

    def do_work(self):
        """子类实现主要工作逻辑"""
        raise NotImplementedError("子类必须实现 do_work()")

    def _on_error(self, error: Exception):
        """错误处理钩子（子类可覆盖）"""
        pass

    def _on_finished(self):
        """完成钩子（子类可覆盖）"""
        pass