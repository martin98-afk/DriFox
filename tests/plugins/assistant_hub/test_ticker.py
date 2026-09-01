# -*- coding: utf-8 -*-
"""test_ticker.py — 记忆调度器测试（fake manager/llm）。"""

import importlib.util
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "core" / "memory" / "ticker.py"

spec = importlib.util.spec_from_file_location("test_ticker_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_ticker_mod", m)
spec.loader.exec_module(m)


class FakeManager:
    """记录 compile_chain/dream/reflect 调用序列的假 manager。"""

    def __init__(self, active_id="a1", llm_error=False):
        self._active_id = active_id
        self.calls = []
        self._llm_error = llm_error
        self.lock = threading.Lock()

    def active_id(self):
        return self._active_id

    def has(self, aid):
        return bool(aid)

    def get(self, aid):
        class _A:
            memory_enabled = True
            dream_auto_enabled = True
            experience_enabled = True
            name = "测试"

        return _A() if aid else None

    def compile_chain(self, aid, *, light=False):
        with self.lock:
            self.calls.append(("compile", aid, light))
        if self._llm_error:
            return {"ok": False, "error": "llm_unavailable"}
        return {"ok": True}

    def dream_start_auto_if_eligible(self, aid, logical_date):
        with self.lock:
            self.calls.append(("dream_auto", aid, logical_date))
        return None

    def experience_reflect(self, aid):
        with self.lock:
            self.calls.append(("reflect", aid))
        return {"added": 0, "items": []}


def _drain(ticker, timeout=3.0):
    """等待工作线程队列清空。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with ticker._pending_lock:
            if not ticker._pending and not ticker._running:
                return True
        time.sleep(0.02)
    return False


def test_turn_counter_triggers_light_chain_every_10(tmp_path):
    mgr = FakeManager()
    t = m.MemoryTicker(mgr, state_dir=tmp_path)
    try:
        for _ in range(9):
            t.on_turn_finished()
        assert not mgr.calls  # 9 轮不触发
        t.on_turn_finished()  # 第 10 轮触发
        assert _drain(t)
        assert ("compile", "a1", True) in mgr.calls
        # 计数持久化
        assert (tmp_path / "turn-state.json").exists()
        # 不重复触发（还差 9 轮）
        t.on_turn_finished()
        assert _drain(t)
        n = len([c for c in mgr.calls if c[0] == "compile"])
        assert n == 1
    finally:
        t.stop()


def test_light_chain_skipped_when_llm_unavailable(tmp_path):
    mgr = FakeManager(llm_error=True)
    t = m.MemoryTicker(mgr, state_dir=tmp_path)
    try:
        for _ in range(m._TURNS_PER_CHAIN):
            t.on_turn_finished()
        assert _drain(t)
        # compile 仍被调用（内部静默降级），但后续 dream/reflect 不再由轻量链触发
        assert ("compile", "a1", True) in mgr.calls
    finally:
        t.stop()


def test_daily_maintenance_sequence(tmp_path):
    mgr = FakeManager()
    t = m.MemoryTicker(mgr, state_dir=tmp_path)
    try:
        t.daily_maintenance(logical_date="2026-09-01")
        assert _drain(t)
        kinds = [c[0] for c in mgr.calls]
        # 顺序铁律：compile（日批）→ dream_auto → reflect
        assert kinds == ["compile", "dream_auto", "reflect"]
        assert mgr.calls[0] == ("compile", "a1", False)
        assert mgr.calls[1] == ("dream_auto", "a1", "2026-09-01")
        # 当天日批只跑一次
        t.daily_maintenance(logical_date="2026-09-01")
        assert _drain(t)
        assert len(mgr.calls) == 3
    finally:
        t.stop()


def test_daily_respects_dream_auto_disabled(tmp_path):
    class M2(FakeManager):
        def get(self, aid):
            class _A:
                memory_enabled = True
                dream_auto_enabled = False  # 关闭
                experience_enabled = True

            return _A() if aid else None

    mgr = M2()
    t = m.MemoryTicker(mgr, state_dir=tmp_path)
    try:
        t.daily_maintenance(logical_date="2026-09-01")
        assert _drain(t)
        kinds = [c[0] for c in mgr.calls]
        assert "dream_auto" not in kinds and "reflect" in kinds
    finally:
        t.stop()


def test_no_active_assistant_noop(tmp_path):
    mgr = FakeManager(active_id="")
    t = m.MemoryTicker(mgr, state_dir=tmp_path)
    try:
        t.on_turn_finished()
        t.daily_maintenance(logical_date="2026-09-01")
        assert _drain(t)
        assert mgr.calls == []
    finally:
        t.stop()
