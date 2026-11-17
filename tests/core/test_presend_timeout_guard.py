# -*- coding: utf-8 -*-
"""I11 回归：发送预处理 QEventLoop 超时兜底。

背景：app/core/engines/ui/engine.py 预处理等待块原为
`loop = QEventLoop(); worker.finished.connect(loop.quit); loop.exec()`，
无任何超时——预处理 worker 卡死（hook 死锁/同步 IO 挂起）时发送路径
永久挂起且 worker 永不 deleteLater。

覆盖：
① 源码守卫：等待块必须含 QTimer 兜底 + 30s 上限 + 超时 error 分支
   （requestInterruption + deleteLater + emit error）——锁死三要素防回退；
② 行为验证（与 engine 等待块同构复刻，connect 先于 start，验证核心 Qt
   语义假设）：
   - 卡死 worker：嵌套 QEventLoop 中 QTimer 到点正常派发 → exec 退出、
     timeout 置位、超时路径 requestInterruption/deleteLater 可执行；
   - 正常 worker：finished 先到 → timer.stop 后 timeout 不误标。
"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtCore", reason="需要 PyQt5 运行时")

from PyQt5.QtCore import QEventLoop, QThread, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

_APP = Path(__file__).resolve().parents[2] / "app" / "core" / "engines" / "ui" / "engine.py"

#: 与 engine.py 等待块同构的小超时值（ms），只为快速验证 Qt 语义
_TEST_TIMEOUT_MS = 60

# offscreen 无窗口环境显式建 app（QTimer 派发 + worker parent 挂靠都需要）
_QAPP = QApplication.instance() or QApplication([])


class _StuckWorker(QThread):
    """可控行为 worker：sleep_s=5 模拟卡死；sleep_s=0 模拟毫秒级正常完成。

    ⚠️ 挂 QApplication parent：run 瞬间结束后若 wrapper 被 GC，QThread 析构
    会踩「Destroyed while thread is still running」竞态 → 0xC0000409
    fail-fast（本项目已知崩溃形态）。挂 parent 保 wrapper 活到进程退出。
    """

    def __init__(self, sleep_s: float):
        super().__init__(_QAPP)
        self._sleep_s = sleep_s

    def run(self):  # noqa: D102 — 故意无中断检查点，模拟卡死形态
        time.sleep(self._sleep_s)


def _wait_like_engine(worker, timeout_ms: int) -> bool:
    """与 engine.py 等待块同构的复刻（connect 先于 start），返回 timeout_hit。

    若 engine.py 实现形态变化（如改轮询），源码守卫（①）会失败提醒同步——
    二者共同锁定「QTimer 兜底」语义。
    """
    loop = QEventLoop()
    worker.finished.connect(loop.quit)  # 必须先于 start（engine 同构，防信号错过）
    timeout_hit = False
    timer = QTimer()
    timer.setSingleShot(True)

    def _on_timeout():
        nonlocal timeout_hit
        timeout_hit = True
        loop.quit()

    timer.timeout.connect(_on_timeout)
    timer.start(timeout_ms)
    worker.start()
    loop.exec()
    timer.stop()
    return timeout_hit


def test_engine_wait_block_has_timeout_guard():
    """① 源码守卫：等待块必须含 QTimer 兜底 + 30s 上限 + 超时安全收尾。"""
    src = _APP.read_text(encoding="utf-8")
    assert "_PRESEND_TIMEOUT_MS = 30_000" in src, "预处理等待必须有 30s 超时上限"
    assert "_presend_timer.timeout.connect(_on_presend_timeout)" in src, "必须有 QTimer 超时兜底"
    assert "_presend_timer.stop()" in src, "正常完成路径必须停表防同拍误标"
    assert "_presend_timeout_hit and worker.isRunning()" in src, "超时判定必须含 worker 仍卡死条件"
    assert "worker.requestInterruption()" in src, "超时必须请求中断（不 terminate）"
    # 卡死线程绝不可立即 deleteLater（派发时线程仍在运行 → qFatal），必须挂
    # finished 延迟回收；正常路径统一 deleteLater 在超时分支之后、_error 之前。
    assert "worker.finished.connect(worker.deleteLater)" in src, "超时分支必须延迟回收卡死 worker"
    guard = src.find("_presend_timeout_hit and worker.isRunning()")
    delayed = src.find("worker.finished.connect(worker.deleteLater)")
    dl = src.rfind("worker.deleteLater()")
    err = src.find("if worker._error:")
    assert guard != -1 and delayed != -1 and dl != -1 and err != -1
    assert guard < delayed, "超时分支内先 requestInterruption 再挂延迟回收"
    assert dl < err, "正常路径统一 deleteLater 必须在 _error 分支之前"


def test_stuck_worker_timeout_exits_loop_and_cleans_up():
    """② 卡死 worker：QTimer 在嵌套 loop 中派发 → 退出 + timeout 置位 + 延迟回收动作可执行。

    与 engine 超时分支同构：requestInterruption + finished→deleteLater 延迟
    回收（卡死线程不可立即 deleteLater——派发时线程仍在运行会 qFatal）。
    """
    worker = _StuckWorker(sleep_s=0.5)
    try:
        t0 = time.monotonic()
        timeout_hit = _wait_like_engine(worker, _TEST_TIMEOUT_MS)
        elapsed = time.monotonic() - t0
        assert timeout_hit is True, "卡死 worker 必须由超时退出等待"
        assert elapsed < 2.0, f"超时必须及时生效（实测 {elapsed:.2f}s）"
        assert worker.isRunning(), "卡死 worker 此刻仍存活（对应 engine 的 error 分支条件）"
        # engine 超时分支动作：请求中断 + finished 延迟回收（不立即 deleteLater）
        worker.requestInterruption()
        worker.finished.connect(worker.deleteLater)
    finally:
        worker.wait(3000)  # run 0.5s 自然退出，中断请求仅促检查点类 worker 自退


def test_normal_worker_finishes_without_timeout_marker():
    """② 正常 worker：finished 先到 → timer.stop 后 timeout 不误标（正常路径行为不变）。"""
    worker = _StuckWorker(sleep_s=0.0)  # 立即完成（毫秒级正常预处理的同构）
    try:
        timeout_hit = _wait_like_engine(worker, 6000)
        assert timeout_hit is False, "正常完成路径不得误标超时"
        assert not worker.isRunning()
    finally:
        worker.wait(3000)
