# -*- coding: utf-8 -*-
"""I13 回归测试：PYTHON 注入型 hook 执行时长观测

覆盖：
1. 慢注入型 hook（>2s）触发 [HookSlowInjected] WARNING（对齐 COMMAND 的
   [HookSlowCommand] 打点口径），字段含 event/type/耗时/timeout 占比。
2. 正常快 hook 无 WARNING（打点仅一次 monotonic 差值，无新增开销路径）。
3. 行为不变性：观测不改变注入型同步执行语义（结果仍返回、顺序不变）。

背景（#A1 发现 #3 观测缺口）：PYTHON 注入型 hook 在主线程发消息/建窗路径
同步执行，此前慢 hook 吃主线程不可见；本轮仅补观测，不做熔断/降级/异步化。
"""

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

import pytest
from loguru import logger

from app.core.hooks.hook_manager import Hook, HookManager


@dataclass
class _LogCapture:
    """loguru WARNING 级捕获器（用完即摘，不污染全局 sink）"""

    records: List[str] = field(default_factory=list)

    def write(self, message):
        self.records.append(str(message))

    def __enter__(self):
        self._sink_id = logger.add(self.write, level="WARNING")
        return self

    def __exit__(self, *exc):
        logger.remove(self._sink_id)
        return False


@pytest.fixture()
def mgr():
    return HookManager()


def _register_injected_hook(mgr: HookManager, fn_name: str, func: Callable, event: str):
    """注册 PYTHON 注入型 hook（add_output_to_context=True → Phase 2 同步执行）

    注意：HookManager 注册表为类级共享状态，用例间靠独立 event 名隔离。
    """
    mgr.register_function(fn_name, func, owner="i13-test")
    hook = Hook(
        id=f"i13-{fn_name}",
        type="python",
        function=fn_name,
        add_output_to_context=True,
        timeout=30,
    )
    mgr.dynamic_register_hook("i13-test", event, hook)
    return hook


def test_slow_injected_hook_emits_warning(mgr):
    """>2s 慢注入型 hook 必须触发 [HookSlowInjected] WARNING"""

    def slow_fn(context: Dict[str, Any], **kwargs):
        time.sleep(2.1)  # 打点阈值 2000ms 硬编码，慢样本取 2.1s
        return "slow output"

    _register_injected_hook(mgr, "i13_slow_fn", slow_fn, event="I13EventSlow")

    with _LogCapture() as cap:
        results = mgr.trigger_event("I13EventSlow", context={}, trigger_async=False)

    assert results and results[0].success, "观测不得改变注入型 hook 执行结果"
    slow_logs = [r for r in cap.records if "[HookSlowInjected]" in r]
    assert slow_logs, ">2s 注入型 hook 必须打 [HookSlowInjected] WARNING"
    log = slow_logs[0]
    assert "I13EventSlow" in log, "WARNING 应含事件名"
    assert "python" in log, "WARNING 应含 hook 类型"
    assert "耗时" in log and "timeout" in log, "WARNING 应对齐 COMMAND 口径（耗时 + timeout 占比）"


def test_fast_injected_hook_no_warning_and_result_intact(mgr):
    """正常快 hook：无 WARNING，结果原样返回（打点无新增开销路径）"""

    def fast_fn(context: Dict[str, Any], **kwargs):
        return "fast output"

    _register_injected_hook(mgr, "i13_fast_fn", fast_fn, event="I13EventFast")

    with _LogCapture() as cap:
        results = mgr.trigger_event("I13EventFast", context={}, trigger_async=False)

    assert results and results[0].success and results[0].output == "fast output", "快 hook 结果不受影响"
    assert not [r for r in cap.records if "[HookSlowInjected]" in r], "快 hook 不得触发慢打点"


def test_two_injected_hooks_keep_order(mgr):
    """多注入型 hook：观测包裹后仍按序执行、结果顺序不变"""

    order = []

    def first(context: Dict[str, Any], **kwargs):
        order.append("first")
        return "out-first"

    def second(context: Dict[str, Any], **kwargs):
        order.append("second")
        return "out-second"

    _register_injected_hook(mgr, "i13_first_fn", first, event="I13EventOrder")
    _register_injected_hook(mgr, "i13_second_fn", second, event="I13EventOrder")

    results = mgr.trigger_event("I13EventOrder", context={}, trigger_async=False)
    assert [r.output for r in results] == ["out-first", "out-second"], "结果顺序不得因打点改变"
    assert order == ["first", "second"], "注入型按序执行语义不变"
