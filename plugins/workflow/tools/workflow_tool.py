# -*- coding: utf-8 -*-
"""workflow 工具 — 受限 Python 脚本编排子智能体。

脚本在受限命名空间内 exec（containment，非安全边界），经钩子扇出子智能体；
agent() 走 SubAgentManager.execute_task + Event 同步等待，子任务自动进任务体系。
"""
from __future__ import annotations

import builtins
import datetime
import json
import math
import re
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import orjson
from loguru import logger

from app.plugins.managers.plugin_config_store import PluginConfigStore
from app.tools.result import ToolResult
from app.tools.registry import make_summarize_from_preview

PLUGIN_NAME = "workflow"

GROUP_SUBAGENT = "子智能体"


class WorkflowError(Exception):
    """钩子误用 / 上限触发（杀全脚本，模型可修正后重发）"""


class WorkflowTimeoutError(WorkflowError):
    """run 总时长超限"""


# 白名单式内置函数（containment：白名单外的名字一律 NameError）
_ALLOWED_BUILTINS: dict = {
    "None": None,
    "True": True,
    "False": False,
}
_ALLOWED_BUILTINS.update(
    (name, getattr(builtins, name))
    for name in (
        "NotImplemented", "Ellipsis",
        "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
        "callable", "chr", "dict", "divmod", "enumerate", "filter", "float",
        "format", "frozenset", "getattr", "hasattr", "hash", "hex", "int",
        "isinstance", "issubclass", "iter", "len", "list", "map", "max",
        "min", "next", "object", "oct", "ord", "pow", "range", "repr",
        "reversed", "round", "set", "setattr", "slice", "sorted", "str",
        "sum", "tuple", "type", "zip",
        "ArithmeticError", "AssertionError", "AttributeError", "Exception",
        "IndexError", "KeyError", "LookupError", "NameError", "TypeError",
        "ValueError", "ZeroDivisionError", "RuntimeError", "StopIteration",
    )
)

# 预置只读常用模块（脚本只做协调与数据变换，不开放 __import__）
_PRESET_MODULES: dict = {
    "json": json,
    "math": math,
    "re": re,
    "statistics": statistics,
    "datetime": datetime,
}


def _build_sandbox(args, hooks: dict | None = None) -> dict:
    """构建受限命名空间：白名单 builtins + 预置只读模块 + 钩子 + args。"""
    ns = {"__builtins__": dict(_ALLOWED_BUILTINS)}
    ns.update(_PRESET_MODULES)
    if hooks:
        ns.update(hooks)
    ns["args"] = args
    return ns


class _RunState:
    """单次 run 的额度与时长状态（线程安全）。"""

    def __init__(self, max_total_agents: int, deadline: float):
        self._lock = threading.Lock()
        self._max_total = max_total_agents
        self._deadline = deadline
        self.started = 0

    def _check_deadline(self) -> None:
        if time.monotonic() > self._deadline:
            raise WorkflowTimeoutError("workflow 超过总时长上限（deadline 已过）")

    def check(self) -> None:
        """只查时长，不计数（parallel/pipeline 入口用）。"""
        self._check_deadline()

    def reserve(self) -> None:
        """检查点：时长超限抛 WorkflowTimeoutError；总数超限抛 WorkflowError；通过则原子计数。"""
        self._check_deadline()
        with self._lock:
            if self.started >= self._max_total:
                raise WorkflowError(f"子智能体总数超上限（{self._max_total}）")
            self.started += 1
