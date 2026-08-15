# -*- coding: utf-8 -*-
"""
window_state 通用窗口级状态容器测试（插件化 M5+：services["window_state"]）

覆盖：
1. window_state_set/get/delete 基本存取 + 默认值 + 覆盖 + 删除返回值
2. 两个 ToolExecutor 实例互不干扰（窗口隔离核心断言）
3. services["window_state"] 注入 → todowrite/todoread 走窗口状态（ToolResult.todos 回传）
4. get_todos/set_todos/clear_todo_list 与 window_state("todo") 联动
5. 无 services 时降级 app.tools.task_state 模块级

运行: python -m pytest tests/test_window_state_isolation.py -v
"""
import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from app.core.tool_executor import ToolExecutor

# 加载 task_tools 插件模块（plugins/ 非 Python 包，_load_module 模式）
_PLUGIN_TOOLS = PROJECT_ROOT / "plugins" / "system" / "tools"


def _load_task_tools():
    mod_name = "_ws_test_task_tools"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_TOOLS / "task_tools.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _new_executor():
    """轻量构造 ToolExecutor（__new__ 绕过 __init__，仅初始化 window_state 容器）。"""
    ex = ToolExecutor.__new__(ToolExecutor)
    ex._window_state = {}
    ex._window_state_lock = threading.Lock()
    return ex


def _ws_services(ex):
    """构造 services["window_state"] 注入字典（与 tool_executor 注入逻辑一致）。"""
    return {
        "window_state": {
            "get": ex.window_state_get,
            "set": ex.window_state_set,
            "delete": ex.window_state_delete,
        }
    }


class TestWindowStateBasic:
    """window_state_set/get/delete 基本存取"""

    def test_set_get_roundtrip(self):
        ex = _new_executor()
        ex.window_state_set("key", "value")
        assert ex.window_state_get("key") == "value"

    def test_get_default_when_missing(self):
        ex = _new_executor()
        assert ex.window_state_get("missing") is None
        assert ex.window_state_get("missing", "dflt") == "dflt"

    def test_overwrite_value(self):
        ex = _new_executor()
        ex.window_state_set("k", 1)
        ex.window_state_set("k", 2)
        assert ex.window_state_get("k") == 2

    def test_delete_returns_removed_value(self):
        ex = _new_executor()
        ex.window_state_set("k", {"a": 1})
        assert ex.window_state_delete("k") == {"a": 1}
        assert ex.window_state_get("k") is None

    def test_delete_missing_returns_none(self):
        ex = _new_executor()
        assert ex.window_state_delete("missing") is None

    def test_dict_value_is_reference_not_copy(self):
        """值按引用存取（与旧 _todo_list 语义一致：容器存引用）。"""
        ex = _new_executor()
        data = [{"content": "x"}]
        ex.window_state_set("todo", data)
        data.append({"content": "y"})
        assert len(ex.window_state_get("todo")) == 2

    def test_thread_safety_concurrent_access(self):
        """并发 set/get 无异常（锁保护）。"""
        ex = _new_executor()
        errors = []

        def worker(i):
            try:
                for j in range(200):
                    ex.window_state_set(f"k{i}", j)
                    ex.window_state_get(f"k{i}")
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors, f"并发访问异常: {errors}"


class TestWindowIsolation:
    """两个 ToolExecutor 实例（两个窗口）状态互不干扰"""

    def test_same_key_different_instances_isolated(self):
        ex1 = _new_executor()
        ex2 = _new_executor()
        ex1.window_state_set("todo", [{"content": "w1"}])
        assert ex1.window_state_get("todo") == [{"content": "w1"}]
        assert ex2.window_state_get("todo") is None, "另一窗口不应读到本窗口状态"

    def test_clear_one_window_keeps_other(self):
        ex1 = _new_executor()
        ex2 = _new_executor()
        ex1.window_state_set("todo", [{"content": "a"}])
        ex2.window_state_set("todo", [{"content": "b"}])
        ex1.window_state_delete("todo")
        assert ex1.window_state_get("todo") is None
        assert ex2.window_state_get("todo") == [{"content": "b"}], "清空窗口1不应影响窗口2"


class TestTodoViaWindowState:
    """services["window_state"] 注入 → todowrite/todoread 走窗口状态"""

    def test_todowrite_reads_back_via_window_state(self):
        task = _load_task_tools()
        ex = _new_executor()
        ctx = {"workdir": None, "session_id": "s", "services": _ws_services(ex)}

        r = task._todowrite_impl(tool_ctx=ctx, todos=[{"content": "t1", "status": "pending"}])
        assert r.success
        assert r.todos and r.todos[0]["content"] == "t1"
        # 状态写入 window_state 容器（而非模块级）
        assert ex.window_state_get("todo")[0]["content"] == "t1"

        r = task._todoread_impl(tool_ctx=ctx)
        assert r.success
        assert r.todos[0]["content"] == "t1"
        assert "t1" in r.content

    def test_todo_isolated_between_windows(self):
        """核心：窗口 A 写 todo 不影响窗口 B（todo 走 window_state 隔离）"""
        task = _load_task_tools()
        ex_a = _new_executor()
        ex_b = _new_executor()
        ctx_a = {"workdir": None, "session_id": "a", "services": _ws_services(ex_a)}
        ctx_b = {"workdir": None, "session_id": "b", "services": _ws_services(ex_b)}

        task._todowrite_impl(tool_ctx=ctx_a, todos=[{"content": "A 的任务", "status": "in_progress"}])
        r_b = task._todoread_impl(tool_ctx=ctx_b)
        assert r_b.success
        assert r_b.todos == [], f"窗口 B 不应读到窗口 A 的 todo，实际: {r_b.todos}"
        assert ex_a.window_state_get("todo")[0]["content"] == "A 的任务"
        assert ex_b.window_state_get("todo") is None

    def test_get_todos_set_todos_clear_linkage(self):
        """get_todos/set_todos/clear_todo_list 与 window_state("todo") 联动"""
        ex = _new_executor()
        # set_todos 归一化后写入 window_state
        normalized = ex.set_todos([{"Content": "x", "STATUS": "DONE", "priority": "HIGH"}])
        assert normalized[0]["status"] == "done"
        assert normalized[0]["priority"] == "high"
        assert ex.window_state_get("todo") == normalized

        # get_todos 从 window_state 读（返回副本）
        assert ex.get_todos() == normalized
        # 副本语义：修改返回值不影响容器
        ex.get_todos().clear()
        assert len(ex.window_state_get("todo")) == 1

        # clear_todo_list 删除 window_state("todo")
        ex.clear_todo_list()
        assert ex.window_state_get("todo") is None
        assert ex.get_todos() == []

    def test_get_todos_default_empty(self):
        ex = _new_executor()
        assert ex.get_todos() == []


class TestFallbackModuleState:
    """无 services（直接调 impl 的测试/无窗口场景）降级 app.tools.task_state 模块级"""

    @pytest.fixture(autouse=True)
    def _clean_module_state(self):
        """清理模块级待办残留（进程级全局，避免污染其他测试）"""
        from app.tools import task_state

        task_state.set_todos([])
        yield
        task_state.set_todos([])

    def test_no_services_falls_back_to_module(self):
        task = _load_task_tools()
        # 无 services["window_state"] → _todo_state 走模块级
        ctx = {"workdir": None, "session_id": "s", "services": {}}
        r = task._todowrite_impl(tool_ctx=ctx, todos=[{"content": "module-level", "status": "pending"}])
        assert r.success
        r = task._todoread_impl(tool_ctx=ctx)
        assert r.success
        assert "module-level" in r.content

    def test_partial_services_without_window_state_falls_back(self):
        task = _load_task_tools()
        ctx = {"workdir": None, "session_id": "s", "services": {"todo": {"get": lambda: [], "set": lambda t: None}}}
        # 旧 services["todo"] 键已废弃：仅 window_state 生效 → 回退模块级，不崩溃
        r = task._todowrite_impl(tool_ctx=ctx, todos=[{"content": "x", "status": "pending"}])
        assert r.success
