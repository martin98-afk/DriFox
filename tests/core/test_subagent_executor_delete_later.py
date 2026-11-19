# -*- coding: utf-8 -*-
"""T11-②：SubAgentExecutor finished → deleteLater 守护测试

接线点：manager.execute_task 内 executor.finished.connect(executor.deleteLater)
（对照 share_card 上传线程先例）。run() 任一出口（含 agent not found 的 error
出口）返回后 Qt 自动发 QThread.finished → C++ 对象析构。
禁止在 pop 点 deleteLater（析构抢跑 OS 线程的 qFatal 窗口）——本测试锁定
接线在 finished 信号上。
"""
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_MANAGER_PATH = PROJECT_ROOT / "plugins" / "system-subagent" / "manager.py"

def _load_manager_mod():
    spec = importlib.util.spec_from_file_location("test_dl_manager", _MANAGER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

@pytest.fixture
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app

class _CounterAgentManager:
    """get_agent 首次返回合法 agent（execute_task 建链），之后返回 None
    （executor.run() 走 agent-not-found error 出口，快速结束线程）"""

    def __init__(self):
        self._calls = 0

    def get_agent(self, name):
        self._calls += 1
        if self._calls == 1:
            a = type(
                "A",
                (),
                {"mode": "subagent", "is_subagent": lambda self: True, "steps": None},
            )()
            return a
        return None

    def get_agent_system_prompt(self, *a, **k):
        return "x"

    def get_agent_tools_schema(self, *a, **k):
        return []

class _NoopExecutor:
    def get_workdir(self):
        return ""

def _spawn(mod, qapp, agent_manager):
    m = mod.SubAgentManager(agent_manager, _NoopExecutor(), lambda: {"模型名称": "x"})
    seen = {"finished": False, "error": ""}
    executor_ref = {}
    ok = m.execute_task(
        "t-dl",
        "some-agent",
        "任务",
        on_finished=lambda tid: seen.__setitem__("finished", True),
        on_error=lambda tid, err: seen.__setitem__("error", err),
        executor_ref=executor_ref,
    )
    return ok, executor_ref, seen

def test_executor_cpp_deleted_after_finish(qapp):
    """wait 后 processEvents：run() 返回 → QThread.finished → deleteLater → C++ 已删"""
    from PyQt5.QtCore import QThread
    try:
        from PyQt5 import sip
    except ImportError:
        import sip
    mod = _load_manager_mod()
    ok, executor_ref, seen = _spawn(mod, qapp, _CounterAgentManager())
    assert ok, "execute_task 应成功建链"
    ex = executor_ref["executor"]
    assert isinstance(ex, QThread)
    assert ex.wait(10000), "executor 线程应在超时内结束"
    qapp.processEvents()
    deadline = 50
    while sip.isdeleted(ex) is False and deadline > 0:
        qapp.processEvents()
        deadline -= 1
    assert sip.isdeleted(ex), "run() 返回后 finished → deleteLater 应已析构 C++ 对象"

def test_executor_deleted_after_error(qapp):
    """error 出口同样触发 deleteLater（agent not found → error_occurred → finished）"""
    try:
        from PyQt5 import sip
    except ImportError:
        import sip
    mod = _load_manager_mod()
    ok, executor_ref, seen = _spawn(mod, qapp, _CounterAgentManager())
    assert ok
    ex = executor_ref["executor"]
    assert ex.wait(10000)
    qapp.processEvents()
    deadline = 50
    while sip.isdeleted(ex) is False and deadline > 0:
        qapp.processEvents()
        deadline -= 1
    assert sip.isdeleted(ex), "error 出口后 deleteLater 应已析构"
