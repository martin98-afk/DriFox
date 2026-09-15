# -*- coding: utf-8 -*-
"""renderer 内存看门狗（RendererWatchdog）回归测试。

背景：长对话 + 大 base64 图场景下，承载可见卡片的 renderer 进程内存单调
涨到 3GB+ → V8 窒息 → 全部视图白屏（含新建对话），且进程未崩溃时
renderProcessTerminated 不触发、自愈链沉睡。看门狗检测病态 PID 并发信号，
由宿主 terminate 唤醒既有自愈链。

覆盖：
* streak 判定：同一 PID 连续超阈达阈值才发信号；低于阈值清零；
* 单次触发后进入全局冷却，冷却期内不重复发；
* PID 消失（进程已死）后计数清理；
* 不监听非 renderer 进程（GPU/utility）。

运行::

    python -m pytest tests/widgets/test_renderer_watchdog.py -v
"""

import os
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.widgets import renderer_watchdog as rw  # noqa: E402
from app.widgets.renderer_watchdog import RendererWatchdog  # noqa: E402


class _FakeProc:
    def __init__(self, pid: int, name: str, cmdline: list, rss_mb: float):
        self.pid = pid
        self._name = name
        self._cmdline = cmdline
        self._rss = rss_mb * 1024 * 1024

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline

    def memory_info(self):
        return types.SimpleNamespace(rss=int(self._rss))


class _FakePsutil:
    """psutil.Process() 返回值替身：children() 返回预设子进程列表。"""

    def __init__(self, children):
        self._children = children

    def children(self, recursive=False):
        return self._children


@pytest.fixture(autouse=True)
def fresh_watchdog(monkeypatch):
    """每个用例重置单例与阈值，避免跨用例污染。"""
    monkeypatch.setattr(rw, "_SICK_RSS_MB", 1000.0)
    monkeypatch.setattr(rw, "_SICK_STREAK", 3)
    monkeypatch.setattr(rw, "_COOLDOWN_S", 600.0)
    RendererWatchdog._instance = None
    w = RendererWatchdog.get_instance()
    yield w
    w.stop()
    RendererWatchdog._instance = None


def _feed(watchdog, children, monkeypatch):
    """注入 psutil 替身后执行一次采样。"""
    fake = types.ModuleType("psutil")
    fake.Process = lambda: _FakePsutil(children)
    fake.NoSuchProcess = Exception
    fake.AccessDenied = Exception
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake)
    watchdog._sample_once()


class TestSickDetection:
    def test_no_emit_below_threshold(self, fresh_watchdog, monkeypatch, qapp):
        fired = []
        fresh_watchdog.rendererSick.connect(lambda pid, mb: fired.append((pid, mb)))
        child = _FakeProc(111, "QtWebEngineProcess.exe", ["x", "--type=renderer"], 500.0)
        for _ in range(5):
            _feed(fresh_watchdog, [child], monkeypatch)
        assert fired == []
        assert fresh_watchdog._streaks == {}

    def test_emit_after_consecutive_streak(self, fresh_watchdog, monkeypatch, qapp):
        fired = []
        fresh_watchdog.rendererSick.connect(lambda pid, mb: fired.append((pid, mb)))
        child = _FakeProc(111, "QtWebEngineProcess.exe", ["x", "--type=renderer"], 1500.0)
        _feed(fresh_watchdog, [child], monkeypatch)
        _feed(fresh_watchdog, [child], monkeypatch)
        assert fired == []  # 未达连续周期数，不发
        _feed(fresh_watchdog, [child], monkeypatch)
        assert fired == [(111, pytest.approx(1500.0, abs=1.0))]

    def test_streak_resets_when_memory_drops(self, fresh_watchdog, monkeypatch, qapp):
        """中间一拍内存回落（如卡片被回收），连续计数必须清零"""
        fired = []
        fresh_watchdog.rendererSick.connect(lambda pid, mb: fired.append((pid, mb)))
        sick = _FakeProc(111, "QtWebEngineProcess.exe", ["x", "--type=renderer"], 1500.0)
        ok = _FakeProc(111, "QtWebEngineProcess.exe", ["x", "--type=renderer"], 500.0)
        _feed(fresh_watchdog, [sick], monkeypatch)
        _feed(fresh_watchdog, [sick], monkeypatch)
        _feed(fresh_watchdog, [ok], monkeypatch)  # 回落 → 清 streak
        _feed(fresh_watchdog, [sick], monkeypatch)
        _feed(fresh_watchdog, [sick], monkeypatch)
        assert fired == []

    def test_non_renderer_children_ignored(self, fresh_watchdog, monkeypatch, qapp):
        """GPU/utility 进程吃再多内存也不判定（它们不承载卡片 DOM）"""
        fired = []
        fresh_watchdog.rendererSick.connect(lambda pid, mb: fired.append((pid, mb)))
        gpu = _FakeProc(222, "QtWebEngineProcess.exe", ["x", "--type=gpu-process"], 9000.0)
        util = _FakeProc(333, "QtWebEngineProcess.exe", ["x", "--type=utility"], 9000.0)
        for _ in range(5):
            _feed(fresh_watchdog, [gpu, util], monkeypatch)
        assert fired == []


class TestCooldownAndCleanup:
    def test_cooldown_blocks_refire(self, fresh_watchdog, monkeypatch, qapp):
        """触发后进入全局冷却：冷却期内即使继续病态也不重复发"""
        fired = []
        fresh_watchdog.rendererSick.connect(lambda pid, mb: fired.append((pid, mb)))
        child = _FakeProc(111, "QtWebEngineProcess.exe", ["x", "--type=renderer"], 1500.0)
        for _ in range(3):
            _feed(fresh_watchdog, [child], monkeypatch)
        assert len(fired) == 1
        # 继续病态 5 拍（远超 streak），冷却期内不得再发
        for _ in range(5):
            _feed(fresh_watchdog, [child], monkeypatch)
        assert len(fired) == 1
        # 冷却过后重新累计满 streak 才再发
        fresh_watchdog._last_fired_at -= rw._COOLDOWN_S + 1.0
        for _ in range(3):
            _feed(fresh_watchdog, [child], monkeypatch)
        assert len(fired) == 2

    def test_dead_pid_streak_cleaned(self, fresh_watchdog, monkeypatch, qapp):
        """PID 消失（进程已死）后计数清理，避免复用旧 PID 的陈旧判定"""
        child = _FakeProc(111, "QtWebEngineProcess.exe", ["x", "--type=renderer"], 1500.0)
        _feed(fresh_watchdog, [child], monkeypatch)
        _feed(fresh_watchdog, [child], monkeypatch)
        assert fresh_watchdog._streaks == {111: 2}
        _feed(fresh_watchdog, [], monkeypatch)  # 进程消失
        assert fresh_watchdog._streaks == {}

    def test_singleton(self, qapp):
        assert RendererWatchdog.get_instance() is RendererWatchdog.get_instance()


class TestEnvKillSwitch:
    def test_disabled_by_env_never_starts(self, monkeypatch, qapp):
        monkeypatch.setattr(rw, "_ENABLED", False)
        fresh_watchdog = RendererWatchdog.get_instance()
        fresh_watchdog.ensure_started()
        assert fresh_watchdog._thread is None
