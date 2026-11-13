# -*- coding: utf-8 -*-
"""system-title-generator UI 组件入口。控制器在主线程创建（QTimer/信号约束）。"""
from loguru import logger

from .generator import create_controller, destroy_controller, get_controller  # noqa: F401

__all__ = ["register_ui", "unload_ui", "get_controller"]


def register_ui(registry):
    create_controller().start()
    logger.info("[system-title-generator] 标题生成控制器已启动")


def unload_ui(registry):
    destroy_controller()
    logger.info("[system-title-generator] 标题生成控制器已停止")
