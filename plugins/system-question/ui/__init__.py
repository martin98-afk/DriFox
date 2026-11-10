# -*- coding: utf-8 -*-
"""system-question UI 组件入口：提问超时控制器生命周期。

控制器必须在主线程创建（QTimer 线程限制）；tools 组件与本组件是独立加载的模块，
本模块只负责 controller 的创建/销毁，不参与工具执行。
"""

from loguru import logger

from .timeout import create_controller, destroy_controller, get_controller  # noqa: F401

__all__ = ["register_ui", "unload_ui", "get_controller"]


def register_ui(registry):
    """启动提问超时控制器（订阅宿主的 question.control 能力）。"""
    create_controller().start()
    logger.info("[system-question] 超时控制器已启动")


def unload_ui(registry):
    """停表 + 退订 + 单例置空（热重载防双控制器并存）。"""
    destroy_controller()
    logger.info("[system-question] 超时控制器已停止")
