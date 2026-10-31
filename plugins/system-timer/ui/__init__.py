# -*- coding: utf-8 -*-
"""system-timer UI 组件入口：register_ui 启动调度器，unload_ui 释放。

调度器必须由本模块在主线程创建（QTimer 线程限制）；tools 组件经
sys.modules["ui_plugin_system_timer"] 解析单例（两个组件是独立加载的模块）。

⚠️ scheduler 符号必须在模块级导入：tools 侧 _resolve_scheduler 从本模块
命名空间 getattr get_scheduler——函数体内 import 是局部绑定，父模块上
没有该符号，工具侧会恒解析 None（「定时调度器未就绪」断裂根因）。
"""

from loguru import logger

from .scheduler import create_scheduler, destroy_scheduler, get_scheduler  # noqa: F401

__all__ = ["register_ui", "unload_ui", "get_scheduler"]


def register_ui(registry):
    """注册 system-timer 的 UI 组件（当前仅后台调度器，无可见界面）。"""
    create_scheduler().start()
    logger.info("[system-timer] UI components registered")


def unload_ui(registry):
    """停 QTimer + 清任务表 + 单例置 None（热重载防双调度器并存）。"""
    destroy_scheduler()
    logger.info("[system-timer] scheduler stopped")
