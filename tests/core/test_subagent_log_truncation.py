# -*- coding: utf-8 -*-
"""H5 回归：task 日志 tool_result 预览截断

锁定三点：
- 超长 tool_result 截到 _LOG_RESULT_PREVIEW_CHARS，带 result_original_len 原长字段
- 短 tool_result 原样保留，不加原长字段
- 非 tool_result 日志不受影响；执行链（raw_result）与日志互不相干由结构保证
  （截断只发生在 _add_log 入口，不触碰工具消息流）
"""

from app.core.workers.subagent_worker import (
    _LOG_ARGS_PREVIEW_CHARS,
    _LOG_RESULT_PREVIEW_CHARS,
    SubAgentExecutor,
)


def _make_executor() -> SubAgentExecutor:
    return SubAgentExecutor(
        task_id="t-h5",
        agent_name="build",
        task_description="H5 截断回归",
        llm_config={},
        agent_manager=object(),
        tool_executor=None,
    )


def test_tool_result_truncated_with_original_len():
    ex = _make_executor()
    big = "x" * (500_000)  # 模拟 read/grep 大结果（T13 实测单条 200KB~数 MB）
    ex._add_log("tool_result", "read", {"result": big, "success": True})
    entry = ex._logs[-1]
    assert entry["result_original_len"] == 500_000
    assert len(entry["result"]) == _LOG_RESULT_PREVIEW_CHARS + len(
        "\n…（结果过长，已截断；全文 500000 字符）"
    )
    assert entry["result"].startswith("x" * 10)


def test_short_tool_result_untouched():
    ex = _make_executor()
    ex._add_log("tool_result", "grep", {"result": "ok", "success": False})
    entry = ex._logs[-1]
    assert entry["result"] == "ok"
    assert "result_original_len" not in entry
    assert entry["success"] is False


def test_non_tool_result_logs_untouched():
    """progress 类日志不截（无 UI 上限对齐项）；thinking/ai_response 的截断
    由 test_ai_response_and_thinking_truncated 单独锁定。"""
    ex = _make_executor()
    huge = "y" * 100_000
    ex._add_log("progress", huge)
    entry = ex._logs[-1]
    assert "result_original_len" not in entry
    assert len(entry["content"]) == 100_000


def test_memory_saving_order_of_magnitude():
    """200 条 1MB 级 tool_result 的日志体量对比：截断前 ~200MB → 截断后 ~0.4MB。"""
    ex = _make_executor()
    for i in range(200):
        ex._add_log("tool_result", "read", {"result": "z" * 1_000_000, "success": True})
    total = sum(len(e.get("result", "")) for e in ex._logs)
    assert total < 200 * (_LOG_RESULT_PREVIEW_CHARS + 200)


# ── H5-2 补漏三类大字段（与 session_card UI 上限逐字对齐）──


def test_ai_response_and_thinking_truncated():
    ex = _make_executor()
    huge = "a" * 50_000
    ex._add_log("ai_response", huge)
    ex._add_log("thinking", huge)
    for entry in ex._logs:
        assert len(entry["content"]) == 10_000 + len("\n…（内容已截断，全文 50000 字符）")
        assert entry["content"].startswith("aaa")


def test_short_ai_response_untouched():
    ex = _make_executor()
    ex._add_log("ai_response", "正常回复")
    assert ex._logs[-1]["content"] == "正常回复"


def test_tool_call_args_tree_truncated_keeps_dict_type():
    """args 逐值截 3000：容器类型必须保持 dict（UI 读侧对 args 做 orjson.dumps）。

    场景取单一大值（总量 < 2×3000），走逐值截断分支；多项叠加的总量上限
    由 test_tool_call_args_total_cap_for_many_items 单独锁定。
    """
    ex = _make_executor()
    args = {"path": "a.py", "content": "b" * 100_000, "nested": {"deep": "c" * 2200}}
    ex._add_log("tool_call", "write", {"args": args})
    saved = ex._logs[-1]["args"]
    import orjson

    assert isinstance(saved, dict)  # 类型契约
    orjson.dumps(saved)  # 可序列化
    assert saved.get("_truncated") is None  # 总量未超限，保持树形结构
    assert 3000 < len(saved["content"]) < 3200
    assert saved["nested"]["deep"] == "c" * 2200  # 短值不动
    assert saved["path"] == "a.py"


def test_tool_call_args_total_cap_for_many_items():
    """[T25] 单值截 3000 不够：多项容器叠加需总量上限（2×3000）。"""
    ex = _make_executor()
    args = {"edits": [{"content": "e" * 2500} for _ in range(20)]}  # 逐值截后 ~50KB 总量
    ex._add_log("tool_call", "multi_edit", {"args": args})
    saved = ex._logs[-1]["args"]
    import orjson

    assert isinstance(saved, dict)
    assert saved.get("_truncated") is True
    assert saved["_original_len"] > _LOG_ARGS_PREVIEW_CHARS * 2
    serialized = orjson.dumps(saved).decode()
    assert len(serialized) < _LOG_ARGS_PREVIEW_CHARS * 2 + 500


def test_cap_args_tree_does_not_mutate_caller_input():
    """[T25] 锁死：_cap_args_tree 必须新建容器，不得原地修改调用方入参——
    否则会污染真实执行参数（raw_result 用的同一 dict）。"""
    from app.core.workers.subagent_worker import _cap_args_tree

    args = {"path": "f.py", "content": "z" * 10_000, "items": ["w" * 5_000, "ok"]}
    snapshot = orjson.dumps(args).decode() if False else repr(args)
    capped = _cap_args_tree(args, 100)
    assert repr(args) == snapshot  # 入参未被修改
    assert len(capped["content"]) < 200  # 截断只发生在新容器上


# ── H5-2/H5-3：manager 归档 32KB 与注入 16KB ──

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

_MANAGER_PATH = Path(__file__).resolve().parents[2] / "plugins" / "system-subagent" / "manager.py"
_mgr_spec = importlib.util.spec_from_file_location("test_subagent_manager_h5", _MANAGER_PATH)
assert _mgr_spec is not None and _mgr_spec.loader is not None
_mgr_mod = importlib.util.module_from_spec(_mgr_spec)
_mgr_spec.loader.exec_module(_mgr_mod)
SubAgentManagerH5 = _mgr_mod.SubAgentManager


class _StoreH5:
    """最小假存储：save 全量 upsert / update_status 部分更新 / get 读取。"""

    def __init__(self):
        self._tasks = {}

    def save(self, task_id, agent_name, task_description, status="running",
             result=None, error=None, logs=None, summary=None, session_id=""):
        self._tasks[task_id] = {
            "task_id": task_id, "agent_name": agent_name,
            "task_description": task_description, "session_id": session_id,
            "status": status, "result": result or "", "error": error or "",
            "logs": logs or [], "summary": summary or {},
        }
        return True

    def update_status(self, task_id, status, result=None, error=None, logs=None, summary=None):
        task = self._tasks.get(task_id)
        if not task:
            return False
        task["status"] = status
        if result is not None:
            task["result"] = result  # DB 存全文（H5-2 前提：DB 全文优先）
        if error is not None:
            task["error"] = error
        if logs is not None:
            task["logs"] = logs
        if summary is not None:
            task["summary"] = summary
        return True

    def get(self, task_id):
        task = self._tasks.get(task_id)
        return dict(task) if task else None


def _make_mgr_h5(store):
    mgr = SubAgentManagerH5(agent_manager=None, tool_executor=None, get_llm_config=lambda: {})
    mgr.set_task_store(store)
    return mgr


def _seed_finished_h5(store, mgr, task_id="t-h5", result_text="R" * 100_000):
    store.save(task_id, "build", "H5", "running", "", "", [], {"tool_call_count": 1}, session_id="s1")
    ex = SubAgentExecutor(
        task_id=task_id, agent_name="build", task_description="H5",
        llm_config={}, agent_manager=None, tool_executor=None,
    )
    ex._last_result = result_text
    ex._task_session_id = "s1"
    ex.isFinished = lambda: True
    mgr._running_tasks[task_id] = ex
    mgr.mark_task_finished(task_id, result_text, "")


def test_archived_result_capped_at_32kb_db_keeps_full():
    store = _StoreH5()
    mgr = _make_mgr_h5(store)
    _seed_finished_h5(store, mgr)
    archived = mgr._finished_tasks["t-h5"]["result"]
    assert len(archived) < 32 * 1024 + 200  # 内存归档截 32KB
    assert "全文 100000 字符" in archived
    assert store._tasks["t-h5"]["result"] == "R" * 100_000  # DB 仍存全文


def test_get_finished_tasks_fallback_archive_capped():
    """[T25 必改 1] 兜底归档路径（get_finished_tasks）同样截 32KB，DB 全文。"""
    store = _StoreH5()
    mgr = _make_mgr_h5(store)
    ex = SubAgentExecutor(
        task_id="t-fb", agent_name="build", task_description="兜底归档",
        llm_config={}, agent_manager=None, tool_executor=None,
    )
    ex._last_result = "F" * 100_000
    ex._task_session_id = "s1"
    ex.isFinished = lambda: True
    mgr._running_tasks["t-fb"] = ex

    finished = mgr.get_finished_tasks()

    assert "t-fb" in finished
    archived = mgr._finished_tasks["t-fb"]["result"]
    assert len(archived) < 32 * 1024 + 200
    assert "全文 100000 字符" in archived
    assert store._tasks["t-fb"]["result"] == "F" * 100_000  # DB 全文优先不受影响


def test_injected_result_capped_at_16kb():
    store = _StoreH5()
    mgr = _make_mgr_h5(store)
    _seed_finished_h5(store, mgr)
    res = mgr.get_tasks_status_with_details(["t-h5"], with_result=True)
    task = res.content["tasks"][0]
    assert task["status"] == "finished"
    assert len(task["result"]) < 16 * 1024 + 200  # 注入上下文截 16KB
    assert "全文 100000 字符" in task["result"]


def test_injected_result_capped_at_16kb_all_active_path():
    """第四处：get_all_active_tasks_with_details 同标准 16KB。

    双层语义：数据源是内存归档（H5-2 已截 32KB），注入层再截 16KB；
    截断提示标注的是注入层输入（归档文本）的原长 32792，任务原始全文
    的原长标记在归档层（32KB 版本内）。
    """
    store = _StoreH5()
    mgr = _make_mgr_h5(store)
    _seed_finished_h5(store, mgr)
    res = mgr.get_all_active_tasks_with_details(with_result=True, session_id="s1")
    tasks = res.content["tasks"]
    assert tasks, "会话 s1 应能查到已归档任务"
    entry = next(t for t in tasks if t["task_id"] == "t-h5")
    assert len(entry["result"]) < 16 * 1024 + 200  # 注入层 ≤16KB
    # [T26] 文案标注归档层原长为下界（32792），防 LLM 低估全文规模
    assert "归档层原长 32792" in entry["result"] and "原始全文可能更长" in entry["result"]
