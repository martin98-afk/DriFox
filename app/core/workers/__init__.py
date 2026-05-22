# -*- coding: utf-8 -*-
"""
Workers 模块 - 包含各种执行器和任务类
"""

from app.core.workers.chat_worker import OpenAIChatWorker
from app.core.workers.cache_tracker import CacheHitRateTracker, CacheStats, AggregatedCacheStats
from app.core.workers.subagent_worker import SubAgentExecutor, SubAgentManager
from app.core.workers.topic_summary import TopicSummaryTask
from app.core.workers.shell_task import ShellExecutionTask
from app.core.workers import error_handler

# AutoLoopWorker 延迟导入（避免 conversation.executor → workers → auto_loop → conversation 循环依赖）


def __getattr__(name):
    if name == "AutoLoopWorker":
        from app.core.workers.auto_loop_worker import AutoLoopWorker
        return AutoLoopWorker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Workers
    "OpenAIChatWorker",
    "SubAgentExecutor",
    "SubAgentManager",
    "AutoLoopWorker",
    # Tasks
    "TopicSummaryTask",
    "ShellExecutionTask",
    # Cache Tracker
    "CacheHitRateTracker",
    "CacheStats",
    "AggregatedCacheStats",
    # Error Handler
    "error_handler",
]