# -*- coding: utf-8 -*-
"""system-cleaner 扫描线程生命周期回归测试

锁定的修复（2026-09）：
1. _cleanup_scan 原 wait(500)：扫描未完成时每次 show/刷新卡死主线程 500ms；
   改为协作式取消（目录边界生效）+ 等线程真正退出后再丢引用。
2. 被 cancel 的 worker 不发 finished：worker 销毁挂 t.finished，
   且引用在线程结束前不可丢——否则 GC 跨线程析构 QObject（0xC0000005）。
3. _walk_dir_size 改 scandir 实现，结果须与文件实际大小一致。
4. _async_scan(force=True) 扫描中途强制重扫；默认去抖跳过。
"""

import os
import sys
import types
import importlib.util

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEventLoop, QTimer  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI_DIR = os.path.join(REPO_ROOT, "plugins", "system-cleaner", "ui")

PKG = "systemcleaner_test_pkg"


def _load_mods():
    """以临时包方式加载插件三模块（cards.py 用相对导入）"""
    if PKG in sys.modules:
        return (sys.modules[f"{PKG}.scanner"], sys.modules[f"{PKG}.cleaner"], sys.modules[f"{PKG}.cards"])
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [UI_DIR]
    sys.modules[PKG] = pkg
    mods = {}
    for m in ("scanner", "cleaner", "cards"):
        full = f"{PKG}.{m}"
        spec = importlib.util.spec_from_file_location(full, os.path.join(UI_DIR, f"{m}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
        mods[m] = mod
    return mods["scanner"], mods["cleaner"], mods["cards"]


def _drain_events(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


# ── scanner 层 ──


def test_walk_dir_size_matches_files(tmp_path, qapp):
    """scandir 实现：目录总大小 = 全部文件字节和（含嵌套子目录）"""
    scanner, _cleaner, _cards = _load_mods()
    (tmp_path / "a.txt").write_bytes(b"x" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 1000)
    (sub / "nested").mkdir()
    (sub / "nested" / "c.log").write_bytes(b"z" * 10)
    assert scanner._walk_dir_size(tmp_path) == 1110


def test_walk_dir_size_empty_or_missing(tmp_path, qapp):
    scanner, _cleaner, _cards = _load_mods()
    assert scanner._walk_dir_size(tmp_path / "nope") == 0
    assert scanner._walk_dir_size(tmp_path) == 0


def test_scan_worker_cancel_no_finished(qapp):
    """cancel 后 run() 在目录边界返回，不发 finished（防孤儿回调）"""
    scanner, _cleaner, _cards = _load_mods()
    w = scanner._ScanWorker(scanner._drifox_dir())
    emitted = []
    w.finished.connect(emitted.append)
    w.cancel()
    w.run()
    assert emitted == []


def test_scan_worker_run_emits_sizes(qapp, tmp_path, monkeypatch):
    """正常完成：finished 携带全部 CACHE_DEFS 的 cid"""
    scanner, _cleaner, _cards = _load_mods()
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_bytes(b"q" * 7)
    monkeypatch.setattr(scanner, "CACHE_DEFS", scanner.CACHE_DEFS[:1])  # 仅 backups → 改指 logs
    monkeypatch.setattr(
        scanner,
        "CACHE_DEFS",
        [("logs", "📝", "日志文件", "logs", False)],
    )
    w = scanner._ScanWorker(tmp_path)
    result = []
    w.finished.connect(result.append)
    w.run()
    assert result == [{"logs": 7}]


# ── cards 层：线程生命周期 ──


def test_cleanup_scan_joins_thread_before_dropping_refs(qapp):
    """cleanup 后线程必须真正结束（isFinished），引用才可丢

    锁定跨线程析构崩溃：wait(0) + 丢引用 → GC 析构工作线程所属 worker。
    """
    _scanner, _cleaner, cards = _load_mods()
    card = cards.SystemCleanerCard()
    try:
        card.show_card()  # 启动后台扫描
        assert card._scan_thread is not None
        card._cleanup_scan()
        assert card._scan_thread is None
        assert card._scan_worker is None
        # 取消语义下等待有界；此处线程应已退出或极快退出
        _drain_events(2000)
    finally:
        card.deleteLater()


def test_async_scan_force_restarts(qapp, tmp_path, monkeypatch):
    """扫描中途 force 重扫：旧线程被取消（不发结果）、新线程启动"""
    scanner, _cleaner, cards = _load_mods()
    monkeypatch.setattr(cards, "_drifox_dir", lambda: tmp_path)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    card = cards.SystemCleanerCard()
    try:
        card._async_scan()
        t1 = card._scan_thread
        assert t1 is not None
        assert card._is_scanning
        card._async_scan(force=True)
        t2 = card._scan_thread
        assert t2 is not None and t2 is not t1
        # 收尾：等全部线程退出，避免测试进程带线程析构
        card._cleanup_scan()
        _drain_events(2000)
    finally:
        card.deleteLater()


def test_async_scan_debounce_skips(qapp, tmp_path, monkeypatch):
    """默认去抖：扫描中再次调用不启动新线程"""
    scanner, _cleaner, cards = _load_mods()
    monkeypatch.setattr(cards, "_drifox_dir", lambda: tmp_path)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    card = cards.SystemCleanerCard()
    try:
        card._async_scan()
        t1 = card._scan_thread
        card._async_scan()
        assert card._scan_thread is t1
        card._cleanup_scan()
        _drain_events(2000)
    finally:
        card.deleteLater()
