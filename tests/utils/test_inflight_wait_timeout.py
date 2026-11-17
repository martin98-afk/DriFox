# -*- coding: utf-8 -*-
"""[I1] _wait_inflight_saves 超时行为回归：默认 2s、超时返回 False、告警日志。

行为级验证（与 test_finalize_flush_async.py 的源码断言互补）：
- 默认参数等待 ≈2s 后返回 False（不再是旧 5s 上限）
- 正常完成路径（inflight 归零）立即返回 True
- 超时路径打出 ERROR 告警（含在飞计数）
"""

import time

import pytest

import app.utils.history_manager as history_module
from app.utils.history_manager import HistoryManager


def _make_manager(monkeypatch, tmp_path) -> HistoryManager:
    monkeypatch.setattr(history_module, "get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(HistoryManager, "_init_storage", lambda self: None)
    return HistoryManager()


def test_wait_inflight_times_out_at_default_2s(monkeypatch, tmp_path):
    """inflight 永不收尾时，默认超时 2s 返回 False 且打 ERROR 告警。

    项目日志走 loguru（非标准 logging），caplog 捕不到，用 loguru sink 捕获。
    """
    from loguru import logger

    manager = _make_manager(monkeypatch, tmp_path)
    manager._register_inflight_save()  # 模拟一个卡死的后台写线程

    errors: list = []
    handler_id = logger.add(errors.append, level="ERROR")
    try:
        started = time.perf_counter()
        ok = manager._wait_inflight_saves()
        elapsed = time.perf_counter() - started
    finally:
        logger.remove(handler_id)

    assert ok is False, "在飞写未完成时必须超时返回 False"
    # 默认 2s：等足 ~2s（旧实现是 5s，此处若回退到 5s 会失败）
    assert 1.9 <= elapsed < 3.5, f"默认超时应为 2s 量级，实际等待 {elapsed:.2f}s"
    assert any("超时" in str(rec) for rec in errors), (
        f"超时路径必须打 ERROR 告警日志，实际捕获: {errors}"
    )

    manager._finish_inflight_save()  # 清理，防污染同实例后续断言


def test_wait_inflight_returns_true_when_finished(monkeypatch, tmp_path):
    """后台写完成后（inflight 归零）等待立即返回 True，不等满超时窗口。"""
    manager = _make_manager(monkeypatch, tmp_path)
    manager._register_inflight_save()

    # 模拟后台写线程 50ms 后收尾并唤醒等待者
    from threading import Timer

    timer = Timer(0.05, manager._finish_inflight_save)
    timer.start()
    try:
        started = time.perf_counter()
        ok = manager._wait_inflight_saves()
        elapsed = time.perf_counter() - started
    finally:
        timer.join()

    assert ok is True, "在飞写收尾后等待方必须被唤醒并返回 True"
    assert elapsed < 1.0, f"正常完成不应等满超时窗口，实际 {elapsed:.2f}s"
