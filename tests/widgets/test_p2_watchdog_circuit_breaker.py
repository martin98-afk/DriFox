# -*- coding: utf-8 -*-
"""P2-1：UI 回调 watchdog 熔断（T8.1 行 16 口径）。

五用例：单次超时 degraded / 滑窗累计 degraded / 连续 3 次自动停用+下次跳过 /
修复后计数清零 / 正常回调零开销。计时经 monkeypatch 假时钟不真等。
"""
import sys
import time
import types

import pytest
from loguru import logger

from app.core import ui_callback_watchdog as wd


@pytest.fixture(autouse=True)
def _no_infobar(monkeypatch):
    """qfluentwidgets.InfoBar 在无 QApplication 时不挂起：仅 InfoBar 挂假（触发降级日志），
    其余符号透传真模块（config.py 的 ConfigItem/BoolValidator 等不受影响）。"""
    import qfluentwidgets as _real

    class _FakeQfw(types.ModuleType):
        def __getattr__(self, name):
            if name == "InfoBar":
                def _boom(*a, **k):
                    raise RuntimeError("no UI in tests")

                return _boom
            return getattr(_real, name)

    monkeypatch.setitem(sys.modules, "qfluentwidgets", _FakeQfw("qfluentwidgets"))


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, delta):
        self.now += delta


@pytest.fixture()
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(wd.time, "monotonic", clock.monotonic)
    return clock


@pytest.fixture()
def log_capture():
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


@pytest.fixture(autouse=True)
def _clean_states():
    wd._STATES.clear()
    yield
    wd._STATES.clear()


def test_single_slow_marks_degraded(fake_clock, log_capture):
    """单次回调 >5s → 标记 degraded + warning（fn 执行期间时钟推进）。"""
    def fn():
        fake_clock.advance(6.0)
        return "ok"

    wrapped = wd.wrap_ui_callback("plug-a", "content:t", fn)
    assert wd.is_degraded("plug-a", "content:t") is False  # 调用前未 degrade
    assert wrapped() == "ok"
    assert wd.is_degraded("plug-a", "content:t") is True
    assert any("单次超时" in r for r in log_capture)


def test_sliding_window_accumulation_marks_degraded(fake_clock, log_capture):
    """滑窗累计 >30s（8×4.2=33.6，单次均 ≤5s）→ 标记 degraded。"""
    def fn():
        fake_clock.advance(4.2)
        return "ok"

    wrapped = wd.wrap_ui_callback("plug-b", "content:w", fn)
    for _ in range(7):
        wrapped()
    assert wd.is_degraded("plug-b", "content:w") is False  # 前 7 次累计 29.4 未超
    wrapped()  # 第 8 次：窗口 8 样本 sum=33.6 > 30
    assert wd.is_degraded("plug-b", "content:w") is True
    assert any("滑动窗口累计超时" in r for r in log_capture)


def test_consecutive_three_disables_and_skips(fake_clock, log_capture):
    """连续 3 次 degrade → 自动写入停用矩阵 + 后续调用跳过（fn 不再执行）。"""
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    saved = list(cfg.disabled_plugin_components.value or [])
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        fake_clock.advance(6.0)
        return "ok"

    wrapped = wd.wrap_ui_callback("plug-c", "content:x", fn)
    try:
        for _ in range(3):
            fake_clock.advance(6.0)
            wrapped()
        assert cfg.disabled_plugin_components.value is not None
        assert "plug-c:content:x" in (cfg.disabled_plugin_components.value or [])
        assert wd.is_disabled("plug-c", "content:x") is True
        # 下次跳过：已停用组件回调直接 no-op（fn 不再执行）
        fake_clock.advance(1.0)
        assert wrapped() is None
        assert calls["n"] == 3
        assert any("已自动停用" in r for r in log_capture)
    finally:
        cfg.disabled_plugin_components.value = saved


def test_recovery_resets_counter(fake_clock):
    """修复恢复：正常回调清零连续计数（清零后需重新累计才停用）。"""
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    saved = list(cfg.disabled_plugin_components.value or [])
    step = {"d": 0.0}

    def fn():
        fake_clock.advance(step["d"])
        return "ok"

    wrapped = wd.wrap_ui_callback("plug-d", "content:y", fn)
    try:
        step["d"] = 6.0
        wrapped()
        wrapped()
        assert wd._state("plug-d", "content:y")["consecutive"] == 2
        # 正常回调（修复）→ 清零
        step["d"] = 0.1
        wrapped()
        assert wd._state("plug-d", "content:y")["consecutive"] == 0
        # 再单次 degrade 也只有 1 次，不停用
        step["d"] = 6.0
        wrapped()
        assert wd._state("plug-d", "content:y")["consecutive"] == 1
        assert wd.is_disabled("plug-d", "content:y") is False
    finally:
        cfg.disabled_plugin_components.value = saved


def test_normal_callback_overhead_under_1ms():
    """正常回调零开销：<1ms（真实时钟，无注入）。"""
    fn = wd.wrap_ui_callback("plug-e", "content:z", lambda: 42)
    t0 = time.perf_counter()
    for _ in range(100):
        assert fn() == 42
    elapsed_per_call = (time.perf_counter() - t0) / 100
    assert elapsed_per_call < 0.001, f"per-call overhead {elapsed_per_call * 1000:.3f}ms"
