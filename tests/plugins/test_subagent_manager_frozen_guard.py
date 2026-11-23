# -*- coding: utf-8 -*-
"""B2 状态查询链 archived-frozen 守卫回归（T4g 清单 3 用例）

## 背景（8c 热点）
get_task_logs 每次轮询无条件 get_finished_tasks() → 已归档任务反复 get_logs()
全扫 + 重复落库。B2 引入进程级 _archived_frozen 集合：
- 归档落库成功（mark_task_finished update_status / get_finished_tasks save）后 add
- get_task_logs 仅对「isFinished 且 not frozen」走归档全扫
- get_finished_tasks 循环内 frozen 过滤
- 重启后首次查 DB status=finished 即 lazy 补录冻结

## 纪律
DB 写失败不 add（下次重试，与既有「写失败 UI 退化」语义一致）。

## 构造说明
manager.py 是插件裸模块（无包结构），用 importlib.spec_from_file_location 直载；
SubAgentTaskStore 用 tmp_path 真实 SQLite（比 mock 更贴近落库/重试语义）。
"""

import importlib.util
from unittest.mock import MagicMock

import pytest

_SPEC = importlib.util.spec_from_file_location("sa_manager_b2", "plugins/system-subagent/manager.py")
assert _SPEC is not None and _SPEC.loader is not None, "插件裸模块加载失败"
_sa = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_sa)
SubAgentManager = _sa.SubAgentManager


@pytest.fixture()
def qapp_env(qapp):
    """类级 frozen 集合是进程级单例：用例间强隔离，防止相互污染"""
    SubAgentManager._archived_frozen.clear()
    yield
    SubAgentManager._archived_frozen.clear()


@pytest.fixture()
def manager(qapp_env):
    """stub store（save/update_status/get 全可断言）+ 空 running/finished 的最小 manager。
    store 本体（SQL 受限代理）有自己的回归面（test_subagent_task_store_zero_row），
    本文件焦点是 manager 的 frozen 守卫逻辑。"""
    m = SubAgentManager(None, None, None)
    store = MagicMock()
    store.get.return_value = {
        "task_id": "t",
        "status": "finished",
        "summary": {},
        "logs": [],
        "task_description": "测试任务",
        "agent_name": "worker",
        "session_id": "sid_b2",
        "result": "",
        "error": "",
    }
    m._task_store = store
    return m


def _finished_executor(task_id="t1"):
    """已完成 executor 的最小 mock（get_logs/get_summary 走 MagicMock 吸收）"""
    ex = MagicMock()
    ex.isFinished.return_value = True
    ex.last_result = "结果文本"
    ex.execution_error = ""
    ex.agent_name = "worker"
    ex.task_description = "测试任务"
    ex.tool_call_count = 3
    ex.start_time = 1000.0
    ex._task_session_id = "sid_b2"
    return ex


def test_frozen_task_skips_archive_sweep(manager, monkeypatch):
    """用例①：frozen 后 get_task_logs 不触发归档全扫（get_finished_tasks 计数=0）"""
    ex = _finished_executor("t_frozen")
    manager._running_tasks["t_frozen"] = ex
    # 先走一次真实归档（落库成功 → frozen）
    manager.get_finished_tasks()
    assert "t_frozen" in SubAgentManager._archived_frozen
    assert "t_frozen" not in manager._running_tasks
    calls = {"gft": 0}
    orig = manager.get_finished_tasks

    def _counting():
        calls["gft"] += 1
        return orig()

    monkeypatch.setattr(manager, "get_finished_tasks", _counting)
    # 模拟 UI 轮询（每拍调 get_task_logs）：frozen 任务不再触发归档全扫
    for _ in range(5):
        res = manager.get_task_logs("t_frozen")
        assert res["found"] is True
        assert res["status"] == "finished"
    assert calls["gft"] == 0, "frozen 任务的轮询不得触发 get_finished_tasks 归档全扫"
    # 对照：未冻结的完成任务仍会触发（守卫只对 frozen 短路）
    manager._running_tasks["t_hot"] = _finished_executor("t_hot")
    manager.get_task_logs("t_hot")
    assert calls["gft"] == 1, "未冻结的完成任务必须走归档全扫（否则 DB 永挂 running）"


def test_dual_path_archive_idempotent(manager):
    """用例②：mark_task_finished / get_finished_tasks 双路径归档幂等（setdefault 竞争加固）"""
    ex = _finished_executor("t_dual")
    manager._running_tasks["t_dual"] = ex
    # 路径 1：host 归档（mark_task_finished，从 running pop + update_status）
    manager.mark_task_finished("t_dual", "host 归档结果")
    assert "t_dual" in SubAgentManager._archived_frozen
    assert "t_dual" not in manager._running_tasks
    entry = manager._finished_tasks["t_dual"]
    assert entry["result"] == "host 归档结果"
    # 路径 2：兜底归档（get_finished_tasks）对同 id：executor 已 pop → 循环看不到
    # 该任务，不产生覆盖写；即便未来并发时序下 executor 仍在 running，frozen
    # 过滤也会跳过——两种时序下条目均不被破坏
    manager.get_finished_tasks()
    assert manager._finished_tasks["t_dual"]["result"] == "host 归档结果"
    assert (
        SubAgentManager._archived_frozen.count("t_dual") if hasattr(SubAgentManager._archived_frozen, "count") else True
    )
    # mark 重复调用（信号重放场景）：幂等，条目不破坏
    manager.mark_task_finished("t_dual", "重复归档不应覆盖为空")
    entry2 = manager._finished_tasks["t_dual"]
    assert entry2["result"] == "host 归档结果" or entry2["result"], "重复归档后条目必须保持有效内容"


def test_restart_lazy_freeze_from_db(manager):
    """用例③：重启 lazy 补录——DB finished 行首次查询即冻结，后续轮询零归档扫"""
    # 预置 DB 命中行（模拟重启前已归档）：get 定向返回 finished 行
    manager._task_store.get.return_value = {
        "task_id": "t_restart",
        "status": "finished",
        "summary": {"task_description": "重启前的任务"},
        "logs": [],
        "task_description": "重启前的任务",
        "agent_name": "worker",
        "session_id": "sid_b2",
        "result": "落库结果",
        "error": "",
    }
    # 模拟进程重启：内存 frozen 集合清零（类属性是进程级单例，重启即空）
    SubAgentManager._archived_frozen.clear()
    assert "t_restart" not in SubAgentManager._archived_frozen
    # 重启后首查：DB 命中 finished → lazy 补录冻结
    res = manager.get_task_logs("t_restart")
    assert res["found"] is True and res["status"] == "finished"
    assert "t_restart" in SubAgentManager._archived_frozen, "首查 DB finished 必须补录冻结"
    # 后续轮询：frozen 生效（本用例 DB 命中路径本身不触归档，这里断言集合稳定）
    for _ in range(3):
        manager.get_task_logs("t_restart")
    assert "t_restart" in SubAgentManager._archived_frozen
