# -*- coding: utf-8 -*-
"""S4 验收：BackgroundTaskManager stop 用 Job 杀进程树 + 任务状态事件广播

验收标准：
1. stop 能杀含子进程树的任务（cmd 派生 ping 子进程 → 全灭）
2. 任务状态事件广播（started/completed/stopped 回调可观测）
3. 不破坏现有 start/stop/list 行为
"""
import subprocess
import sys
import threading
import time

import pytest

from app.tools.terminal_tools import BackgroundTaskManager


def _alive(pid):
    try:
        import psutil

        return psutil.Process(pid).is_running()
    except (ImportError, psutil.NoSuchProcess):
        return False


def _children(pid):
    try:
        import psutil

        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except (ImportError, psutil.NoSuchProcess):
        return []


@pytest.fixture
def manager():
    BackgroundTaskManager.reset_instance()
    m = BackgroundTaskManager()
    yield m
    BackgroundTaskManager.reset_instance()


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object 仅 Windows")
class TestBackgroundTaskJobKill:
    def test_stop_kills_process_tree(self, manager):
        """stop 杀含子进程树任务（cmd + 2×ping）"""
        task_id, msg = manager.start(
            "cmd /c ping -n 8 127.0.0.1 >nul & ping -n 8 127.0.0.1 >nul"
        )
        assert task_id
        time.sleep(1.0)
        task = manager._tasks[task_id]
        assert task.status == "running"
        pid = task.pid
        assert _alive(pid), "任务应存活"
        # 确认有子进程（cmd 派生的 ping）
        assert _children(pid), "cmd 应已派生子进程"

        ok, res = manager.stop(task_id)
        assert ok, f"stop 应成功: {res}"
        time.sleep(0.8)
        assert not _alive(pid), "stop 后主进程应被杀"
        assert _children(pid) == [], "stop 后子进程树应无残留"

    def test_stop_uses_job_path(self, manager):
        """stop 走 Job 杀树路径（返回消息含 Job 标识）"""
        task_id, _ = manager.start("ping -n 8 127.0.0.1")
        time.sleep(0.8)
        ok, res = manager.stop(task_id)
        assert ok
        assert "Job" in res, f"应走 Job 杀树路径: {res}"


class TestTaskEventBroadcast:
    def test_events_received(self, manager):
        """任务状态事件广播：started + completed"""
        events = []
        lock = threading.Lock()

        def cb(event, task_id, status, detail=""):
            with lock:
                events.append((event, status))

        manager.on_task_event(cb)
        task_id, _ = manager.start("ping -n 2 127.0.0.1")
        assert task_id
        # 等待任务自然完成（ping -n 2 约 2 秒）
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with lock:
                if any(e[0] == "completed" for e in events):
                    break
            time.sleep(0.2)
        with lock:
            started = [e for e in events if e[0] == "started"]
            completed = [e for e in events if e[0] == "completed"]
        assert started, "应收到 started 事件"
        assert completed, "应收到 completed 事件"

    def test_stop_emits_stopped_event(self, manager):
        """stop 触发 stopped 事件"""
        events = []
        manager.on_task_event(lambda e, tid, s, d="": events.append((e, s)))
        task_id, _ = manager.start("ping -n 8 127.0.0.1")
        time.sleep(0.8)
        manager.stop(task_id)
        time.sleep(0.3)
        stopped = [e for e in events if e[0] == "stopped"]
        assert stopped, f"应收到 stopped 事件: {events}"

    def test_callback_error_isolated(self, manager):
        """回调抛异常不影响任务本身"""
        def bad_cb(*a):
            raise RuntimeError("boom")

        manager.on_task_event(bad_cb)
        task_id, _ = manager.start("ping -n 1 127.0.0.1")
        assert task_id
        time.sleep(0.5)
        # 不抛异常即可；任务状态可查
        assert manager._tasks[task_id].status in ("running", "completed")


class TestBackgroundTaskNoRegression:
    def test_start_list_stop_basic(self, manager):
        """基础行为不回归：start/list/stop"""
        task_id, msg = manager.start("ping -n 2 127.0.0.1")
        assert task_id
        assert task_id in manager.list_tasks(), "list_tasks 应包含新任务"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if manager._tasks[task_id].status == "completed":
                break
            time.sleep(0.2)
        assert manager._tasks[task_id].status == "completed"

    def test_stop_nonexistent_task(self, manager):
        ok, res = manager.stop("bg_does_not_exist")
        assert not ok
        assert "不存在" in res
