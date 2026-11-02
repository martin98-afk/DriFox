# -*- coding: utf-8 -*-
"""system-subagent：会话重载的历史数据提供者

覆盖：task_ids 提取（JSON / Python repr / 截断退化）、已完成过滤、
内存归档回退、窗口服务缺失静默。

加载方式：直接按文件路径载入 ``history_loader.py``（它无相对导入），不经
``ui/__init__.py``——后者会连带 import ``session_card`` → QtWebEngine，在已建
QApplication 的整组跑里会触发 WebEngine 的 import 顺序错误。
"""

import importlib.util
import os
import sys

_MOD_NAME = "system_subagent_history_loader"


def _load():
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    path = os.path.join(os.path.abspath("."), "plugins", "system-subagent", "ui", "history_loader.py")
    spec = importlib.util.spec_from_file_location(_MOD_NAME, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


_mod = _load()


class _FakeManager:
    def __init__(self, tasks):
        self._tasks = tasks
        self.calls = []

    def get_task_logs(self, task_id):
        self.calls.append(task_id)
        return self._tasks.get(task_id, {"found": False})


class _FakeHost:
    def __init__(self, window_id="w1"):
        self._window_id = window_id


def _tool_msg(content, name="subagent_para"):
    return {"role": "tool", "name": name, "content": content, "success": True}


def _patch_manager(monkeypatch, manager):
    monkeypatch.setattr(_mod, "_manager_for", lambda host: manager)


# ──────────────────── task_ids 提取 ────────────────────


def test_extract_task_ids_from_json():
    msg = _tool_msg('{"task_ids": ["a1", "b2"]}')

    assert _mod._extract_task_ids(msg) == ["a1", "b2"]


def test_extract_task_ids_from_python_repr():
    """ToolResult.content（dict）经 str() 落库后是单引号 repr，json.loads 解不了。"""
    msg = _tool_msg("{'task_ids': ['a1'], 'status': 'running'}")

    assert _mod._extract_task_ids(msg) == ["a1"]


def test_extract_task_ids_falls_back_to_uuid_scan():
    """结果被截断/包裹说明文字时仍要能认出任务 id。"""
    text = "已派发 3 个任务\n- 3f2a1c7e-1111-4222-8333-444455556666\n- agent_1a2b3c4d"
    msg = _tool_msg(text)

    assert _mod._extract_task_ids(msg) == ["3f2a1c7e-1111-4222-8333-444455556666", "agent_1a2b3c4d"]


def test_extract_task_ids_empty_content():
    assert _mod._extract_task_ids(_tool_msg("")) == []


# ──────────────────── 行数据组装 ────────────────────


def test_load_history_items_builds_rows(monkeypatch):
    manager = _FakeManager(
        {
            "t1": {
                "found": True,
                "status": "finished",
                "agent_name": "coder",
                "task_description": "写代码",
                "summary": {
                    "agent_name": "coder",
                    "task_description": "写代码",
                    "tool_call_count": 4,
                    "elapsed_seconds": 90,
                    "model_name": "gpt-4o",
                    "context_usage": "12.5K tokens",
                },
            }
        }
    )
    _patch_manager(monkeypatch, manager)

    items = _mod.load_history_items([_tool_msg('{"task_ids": ["t1"]}')], _FakeHost())

    assert items == [
        {
            "task_id": "t1",
            "agent_name": "coder",
            "description": "写代码",
            "model_name": "gpt-4o",
            "tool_call_count": 4,
            "elapsed_seconds": 90,
            "context_usage": "12.5K tokens",
            "success": True,
        }
    ]


def test_load_history_items_failure_marks_not_success(monkeypatch):
    manager = _FakeManager(
        {
            "t1": {
                "found": True,
                "status": "cancelled",
                "agent_name": "coder",
                "summary": {"error": "用户已手动中止子智能体"},
            }
        }
    )
    _patch_manager(monkeypatch, manager)

    items = _mod.load_history_items([_tool_msg('{"task_ids": ["t1"]}')], _FakeHost())

    assert items[0]["success"] is False


def test_load_history_items_skips_running(monkeypatch):
    """运行中的任务由桥接层投影，历史路径不补行（避免重启后残留假运行态）。"""
    manager = _FakeManager({"t1": {"found": True, "status": "running", "summary": {}}})
    _patch_manager(monkeypatch, manager)

    assert _mod.load_history_items([_tool_msg('{"task_ids": ["t1"]}')], _FakeHost()) == []


def test_load_history_items_ignores_non_subagent_tools(monkeypatch):
    manager = _FakeManager({})
    _patch_manager(monkeypatch, manager)

    batch = [_tool_msg('{"task_ids": ["t1"]}', name="read")]

    assert _mod.load_history_items(batch, _FakeHost()) == []
    assert manager.calls == []


def test_load_history_items_without_manager_returns_empty(monkeypatch):
    monkeypatch.setattr(_mod, "_manager_for", lambda host: None)

    assert _mod.load_history_items([_tool_msg('{"task_ids": ["t1"]}')], _FakeHost()) == []


def test_load_history_items_dedupes_task_ids(monkeypatch):
    manager = _FakeManager({"t1": {"found": True, "status": "finished", "summary": {}}})
    _patch_manager(monkeypatch, manager)

    batch = [_tool_msg('{"task_ids": ["t1"]}'), _tool_msg('{"task_ids": ["t1"]}')]

    assert len(_mod.load_history_items(batch, _FakeHost())) == 1
    assert manager.calls == ["t1"]
