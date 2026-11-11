# -*- coding: utf-8 -*-
"""Worker 硬性寿命守卫 — MAX_WORKER_LIFETIME_S / MAX_CONTINUATIONS 回归。

修复背景：默认 LoopPolicy（max_rounds=None + stop_hook_injected → CONTINUE）下，
Stop hook 与退出前 hook 队列注入反复 ``continue`` 续命，``run()`` 永不走正常完成
路径 → ``finished`` 永不发出 → executor T6-A 收尾与 engine_session 残留回收均无
触发点（实测单 worker 最长存活 4511s）。

修复语义：超限**必须走正常完成路径**（emit finished_with_messages +
finished_with_content），禁止直接 return 跳过收尾。

阈值三条语义（MAX_WORKER_LIFETIME_S / MAX_CONTINUATIONS 两个参数统一）：
``0`` = 显式关闭限制；正数 = 生效；负数 / 非数字 / nan / inf = 非法，回退默认并
WARNING。此处的 ``0`` 只涉及寿命/续轮，与权限审批路径的 ``0`` 语义无关。

范围说明：question / 权限两处等待本轮确认**保持无上限**（原始无限轮询，见
``test_wiring_question_and_permission_wait_unlimited``）。寿命守卫位于 run()
主循环顶部，卡在嵌套等待循环时回不到顶部——守卫管「持续续命」，管不了
「卡在等待」；这两处属已知的潜伏风险而非已发生故障，不在本轮处理。

注：守卫内联在 run() 主循环里（与 _check_loop_round_limit 调用点同构），现有测试
基建无 run() 全循环驱动；本文件用「判定函数行为级 + 接线源码级」双层覆盖，与
test_worker_loop_policy_integration 的「代码审查级断言」惯例一致。
"""

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QObject

_ROOT = Path(__file__).resolve().parents[2]

def _make_worker():
    """__new__ 桩：绕过 __init__，仅 QObject 初始化（访问 Signal 描述器需要）。"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    w = worker_cls.__new__(worker_cls)
    QObject.__init__(w)
    return w

# ── 判定函数行为级 ────────────────────────────────────────────────────

def test_reason_empty_for_fresh_stub():
    """干净桩（未跑 run、无 _run_started_at）→ 未超限。"""
    w = _make_worker()
    assert w._worker_lifetime_reason() == ""

def test_reason_empty_when_under_limits():
    """寿命与续轮均在限内 → 空串。"""
    w = _make_worker()
    w._run_started_at = time.monotonic() - 1.0  # 远小于 3600s
    w._continuation_count = 0
    assert w._worker_lifetime_reason() == ""

def test_reason_reports_lifetime_exceeded():
    """总寿命超限 → 返回原因（含「总寿命」）。"""
    w = _make_worker()
    w.MAX_WORKER_LIFETIME_S = 10.0
    w._run_started_at = time.monotonic() - 11.0
    w._continuation_count = 0
    reason = w._worker_lifetime_reason()
    assert reason
    assert "总寿命" in reason

def test_reason_reports_continuation_exceeded():
    """续轮次数超限（> MAX_CONTINUATIONS）→ 返回原因（含「续命」）。"""
    w = _make_worker()
    w._run_started_at = time.monotonic()  # 寿命未超
    w.MAX_CONTINUATIONS = 4
    w._continuation_count = 5
    reason = w._worker_lifetime_reason()
    assert reason
    assert "续命" in reason

def test_reason_boundary_equal_is_not_exceeded(monkeypatch):
    """边界：恰好等于上限不超限（> 语义）。

    monkeypatch time.monotonic 返回确定值：真实 monotonic 两次调用间必有
    微秒级流逝，elapsed 会略大于 100 而稳定翻红（实测踩过），必须消除竞态。
    """
    w = _make_worker()
    w.MAX_WORKER_LIFETIME_S = 100.0
    w._run_started_at = 1000.0
    monkeypatch.setattr("time.monotonic", lambda: 1100.0)  # elapsed 恰等于 100
    w.MAX_CONTINUATIONS = 4
    w._continuation_count = 4
    assert w._worker_lifetime_reason() == ""

def test_reason_zero_threshold_means_disabled():
    """阈值 0 = 显式关闭限制 → 永不超限。"""
    w = _make_worker()
    w.MAX_WORKER_LIFETIME_S = 0.0
    w._run_started_at = time.monotonic() - 10**9  # 极长寿命
    w.MAX_CONTINUATIONS = 0
    w._continuation_count = 10**9
    assert w._worker_lifetime_reason() == ""

def test_zero_lifetime_keeps_continuation_guard(monkeypatch):
    """P0 单边耦合回归：寿命设 0 只关寿命，续轮守卫必须独立生效。

    修复前 `lifetime_s == 0: return ""` 提前返回，连带关掉续轮检查——
    用户只想关寿命，防挂死主守卫被顺手关掉且无任何日志。
    """
    w = _make_worker()
    w.MAX_WORKER_LIFETIME_S = 0.0
    w._run_started_at = time.monotonic() - 10**9  # 寿命维度早已超限，但已关闭
    w.MAX_CONTINUATIONS = 8
    w._continuation_count = 9
    reason = w._worker_lifetime_reason()
    assert reason
    assert "续命" in reason

def test_zero_continuation_keeps_lifetime_guard(monkeypatch):
    """反向组合：续轮设 0 只关续轮，寿命守卫必须独立生效。"""
    w = _make_worker()
    w.MAX_WORKER_LIFETIME_S = 3600.0
    w._run_started_at = 1000.0
    monkeypatch.setattr("time.monotonic", lambda: 5000.0)  # elapsed 4000 > 3600
    w.MAX_CONTINUATIONS = 0
    w._continuation_count = 10**9  # 续轮维度超限，但已关闭
    reason = w._worker_lifetime_reason()
    assert reason
    assert "总寿命" in reason

def test_reason_negative_threshold_falls_back_and_guards(monkeypatch):
    """负数阈值 = 非法 → 回退默认 3600 并正常守卫（不是关闭）。

    确定性时间源：elapsed 固定 4000s > 回退后的 3600s → 必须报超限。
    """
    w = _make_worker()
    w.MAX_WORKER_LIFETIME_S = -5.0
    w._run_started_at = 1000.0
    monkeypatch.setattr("time.monotonic", lambda: 5000.0)
    w._continuation_count = 0
    reason = w._worker_lifetime_reason()
    assert reason
    assert "总寿命" in reason

def test_reason_nan_inf_threshold_falls_back_and_guards(monkeypatch):
    """nan / inf 阈值 = 非法 → 回退默认 3600 并正常守卫（不能静默失效）。"""
    for bad in (float("nan"), float("inf")):
        w = _make_worker()
        w.MAX_WORKER_LIFETIME_S = bad
        w._run_started_at = 1000.0
        monkeypatch.setattr("time.monotonic", lambda: 5000.0)
        w._continuation_count = 0
        reason = w._worker_lifetime_reason()
        assert reason, bad
        assert "总寿命" in reason, bad

def test_reason_lifetime_takes_priority():
    """寿命与续轮同时超限 → 报寿命（先查寿命）。"""
    w = _make_worker()
    w.MAX_WORKER_LIFETIME_S = 10.0
    w._run_started_at = time.monotonic() - 11.0
    w.MAX_CONTINUATIONS = 1
    w._continuation_count = 99
    assert "总寿命" in w._worker_lifetime_reason()

# ── 常量与环境变量 ────────────────────────────────────────────────────

def test_default_constants():
    """默认值：总寿命 3600s、续轮上限 8（两者默认生效；无 deadline 类参数）。"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    assert worker_cls.MAX_WORKER_LIFETIME_S == 3600.0
    assert worker_cls.MAX_CONTINUATIONS == 8

def test_env_override_and_invalid_fallback(monkeypatch):
    """环境变量覆盖生效；非法值回退默认（import 不得崩溃）。"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker")
    monkeypatch.setenv("DRIFOX_WORKER_MAX_LIFETIME_S", "120")
    monkeypatch.setenv("DRIFOX_WORKER_MAX_CONTINUATIONS", "2")
    assert worker_cls._env_float("DRIFOX_WORKER_MAX_LIFETIME_S", 3600.0) == 120.0
    assert worker_cls._env_int("DRIFOX_WORKER_MAX_CONTINUATIONS", 8) == 2
    monkeypatch.delenv("DRIFOX_WORKER_MAX_LIFETIME_S", raising=False)
    monkeypatch.delenv("DRIFOX_WORKER_MAX_CONTINUATIONS", raising=False)
    assert worker_cls._env_float("DRIFOX_WORKER_MAX_LIFETIME_S", 3600.0) == 3600.0
    assert worker_cls._env_int("DRIFOX_WORKER_MAX_CONTINUATIONS", 8) == 8

def test_env_float_zero_means_disabled_negative_falls_back(monkeypatch):
    """_env_float 三语义：0 → 0.0（关闭，不回退）；负数 → 非法回退 default。

    若误用 ``val <= 0`` 判非法，用户显式设 0 关闭会被静默改回默认——
    本用例钉死 ``0`` 必须原样通过。
    """
    worker_cls = pytest.importorskip("app.core.workers.chat_worker")
    monkeypatch.setenv("DRIFOX_WORKER_MAX_LIFETIME_S", "0")
    assert worker_cls._env_float("DRIFOX_WORKER_MAX_LIFETIME_S", 3600.0) == 0.0
    monkeypatch.setenv("DRIFOX_WORKER_MAX_LIFETIME_S", "-5")
    assert worker_cls._env_float("DRIFOX_WORKER_MAX_LIFETIME_S", 3600.0) == 3600.0

def test_env_float_rejects_non_finite(monkeypatch):
    """nan/inf 能被 float() 接受但会让守卫静默失效（elapsed > nan 恒 False）
    → 必须回退 default。"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker")
    for bad in ("nan", "inf", "-inf"):
        monkeypatch.setenv("DRIFOX_WORKER_MAX_LIFETIME_S", bad)
        assert worker_cls._env_float("DRIFOX_WORKER_MAX_LIFETIME_S", 3600.0) == 3600.0, bad

def test_env_int_zero_means_disabled_negative_and_garbage_fall_back(monkeypatch):
    """_env_int：0 → 0（关闭）；nan/inf/1.5/abc/-2 → 回退 default。"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker")
    monkeypatch.setenv("DRIFOX_WORKER_MAX_CONTINUATIONS", "0")
    assert worker_cls._env_int("DRIFOX_WORKER_MAX_CONTINUATIONS", 8) == 0
    for bad in ("nan", "inf", "1.5", "abc", "-2"):
        monkeypatch.setenv("DRIFOX_WORKER_MAX_CONTINUATIONS", bad)
        assert worker_cls._env_int("DRIFOX_WORKER_MAX_CONTINUATIONS", 8) == 8, bad

# ── 接线源码级（守卫必须在标定位置，超限必须走完成序列）──────────────

def _source() -> str:
    return (_ROOT / "app" / "core" / "workers" / "chat_worker.py").read_text(encoding="utf-8")

def test_wiring_run_entry_records_lifetime_start():
    """run() 入口初始化 _run_started_at 与 _continuation_count（与 _loop_round_count 同点）。"""
    src = _source()
    anchor = src.index("self._loop_round_count = 0")
    block = src[anchor : anchor + 400]
    assert "self._run_started_at = time.monotonic()" in block
    assert "self._continuation_count = 0" in block

def test_wiring_lifetime_guard_emits_finished_sequence():
    """while 顶部守卫超限分支必须 emit finished 两组信号（正常完成路径），不得裸 return。"""
    src = _source()
    anchor = src.index("_lt_reason = self._worker_lifetime_reason()")
    block = src[anchor : anchor + 800]
    assert "finished_with_messages" in block
    assert "finished_with_content" in block
    assert "_clear_pending_response_state" in block

def test_wiring_stop_hook_continue_counts_continuation():
    """Stop hook CONTINUE 分支必须累加 _continuation_count（续命计数入口之一）。"""
    src = _source()
    anchor = src.index("if _lp_decision is LoopDecision.CONTINUE:")
    block = src[anchor : anchor + 300]
    assert "_continuation_count" in block
    assert 'getattr(self, "_continuation_count", 0) + 1' in block

def test_wiring_drain_guard_rejects_when_exceeded():
    """hook 队列续命守卫：超限走完成路径，未超限才 continue。"""
    src = _source()
    anchor = src.index("if self._drain_pending_hooks_before_exit(current_session_messages):")
    block = src[anchor : anchor + 1500]
    cont_idx = block.index("continue")
    assert "_worker_lifetime_reason" in block
    # continue 必须在守卫的 else 分支内（守卫判定之后）
    assert block.index("_worker_lifetime_reason") < cont_idx
    assert "finished_with_messages" in block  # 落穿目标：下方正常完成路径

def test_wiring_question_and_permission_wait_unlimited():
    """question / 权限等待保持原始无限轮询（无 deadline），且无 deadline 残留。

    已知潜伏风险（非本轮范围）：卡在嵌套等待循环时回不到主循环顶部，
    寿命守卫管不了「卡在等待」。此用例把「无上限」钉成显式现状，
    防止未来有人误以为守卫已覆盖。
    """
    src = _source()
    for forbidden in ("QUESTION_WAIT_DEADLINE_S", "PERMISSION_WAIT_DEADLINE_S", "_q_deadline", "_perm_deadline"):
        assert forbidden not in src, forbidden
    q_anchor = src.index("while self._pending_answer is None and not self._is_cancelled:")
    q_block = src[q_anchor : q_anchor + 300]
    assert "_answer_event.wait(timeout=1.0)" in q_block
    perm_anchor = src.index("time.sleep(0.1)  # 保留 sleep 用于轮询等待用户授权")
    perm_block = src[perm_anchor - 400 : perm_anchor]
    assert "self._permission_pending is not None" in perm_block
