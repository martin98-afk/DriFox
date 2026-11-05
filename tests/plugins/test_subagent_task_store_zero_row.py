# -*- coding: utf-8 -*-
"""SubAgentTaskStore.update_status 零行更新告警回归测试（工单 F14a）

背景：db_manager.execute 对 UPDATE 返回 (True, rowcount)，0 行=(True,0)
完全静默；update_status 只判 ok，task_id 不存在（fresh db 直更 / 已被
clear_old 并发清除）时无任何痕迹，问题无从排查。

修复：第二返回值改名义 affected，affected == 0 时 warning（只告警不造幽灵行）。
"""

import importlib.util
from pathlib import Path

import pytest
from loguru import logger

_MANAGER_PATH = Path(__file__).resolve().parents[2] / "plugins" / "system-subagent" / "manager.py"
_spec = importlib.util.spec_from_file_location("f14a_subagent_manager", _MANAGER_PATH)
assert _spec is not None and _spec.loader is not None
_manager_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_manager_mod)
SubAgentTaskStore = _manager_mod.SubAgentTaskStore


class _WarnCapture:
    """loguru warning 捕获（项目用 loguru，pytest caplog 不抓）"""

    def __init__(self):
        self.msgs = []

    def __call__(self, message):
        self.msgs.append(str(message))

    def any_with(self, keyword):
        return any(keyword in m for m in self.msgs)


@pytest.fixture
def store(tmp_path):
    from app.plugins.capabilities.providers.db_guard import PluginDBProxy
    from app.utils.db_manager import DatabaseManager

    DatabaseManager._instance = None
    dbm = DatabaseManager()
    dbm.connect(str(tmp_path / "f14a.sqlite"))
    yield SubAgentTaskStore(PluginDBProxy("system-subagent"))
    try:
        dbm.close()
    except Exception:
        pass
    DatabaseManager._instance = None


@pytest.fixture
def warnings():
    capture = _WarnCapture()
    sink_id = logger.add(capture, level="WARNING")
    yield capture
    logger.remove(sink_id)


def _save(store, task_id="t1"):
    store.save(
        task_id,
        "explore",
        "探查代码",
        status="running",
        logs=[{"type": "progress", "content": "开始"}],
        summary={"n": 1},
        session_id="sess-1",
    )


def test_update_missing_id_warns_no_raise(store, warnings):
    """①fresh db 直接 update_status 不存在的 id → 不抛异常 + warning"""
    store.update_status("ghost-id", "finished", result="x")
    assert warnings.any_with("ghost-id")
    assert warnings.any_with("零行更新")
    assert store.get("ghost-id") is None  # 只告警，不造幽灵行


def test_zero_row_after_clear_old_warns(store, warnings):
    """②save → clear_old(days=0) 删光 → update_status → 0 行告警"""
    _save(store)
    assert store.clear_old(days=0) >= 1
    store.update_status("t1", "finished", result="done")
    assert warnings.any_with("零行更新")


def test_normal_path_no_regression(store, warnings):
    """③正常 save→update_status→get 全字段断言；且不产生零行告警"""
    _save(store)
    store.update_status(
        "t1",
        "finished",
        result="最终结果",
        error=None,
        logs=[{"type": "tool_call", "content": "grep"}],
        summary={"tool_calls": 3},
    )
    assert not warnings.any_with("零行更新")
    task = store.get("t1")
    assert task is not None
    assert task["status"] == "finished"
    assert task["result"] == "最终结果"
    assert task["summary"] == {"tool_calls": 3}
    assert task["logs"] == [{"type": "tool_call", "content": "grep"}]
    # None 字段不覆盖：error 保持落库时的空串
    assert task["error"] == ""
    assert task["session_id"] == "sess-1"
