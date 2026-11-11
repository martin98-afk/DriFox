# -*- coding: utf-8 -*-
"""system-cleaner 禁杀当前进程活 WebEngine 后代的回归测试

背景（2026-11-06）：_kill_qwebengine_processes 原实现含「杀当前进程全部后代」
分支，DriFox 正在渲染消息时 WebEngine renderer/GPU/network 是活着的必需后代，
被一并 terminate 导致在途渲染内容直接消失。修复后只保留孤儿清理（父进程已
消失才杀）。

本文件锁定：
1. 当前进程的存活 WebEngine 后代绝不被杀（哪怕出现在 tasklist 枚举里）；
2. 真孤儿（父进程消失）仍被正常清理（孤儿语义不回归）。
"""

import importlib.util
import os
import subprocess
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI_DIR = os.path.join(REPO_ROOT, "plugins", "system-cleaner", "ui")
PKG = "systemcleaner_active_child_pkg"


def _load_scanner():
    if PKG in sys.modules:
        return sys.modules[f"{PKG}.scanner"]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [UI_DIR]
    sys.modules[PKG] = pkg
    spec = importlib.util.spec_from_file_location(
        f"{PKG}.scanner", os.path.join(UI_DIR, "scanner.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.scanner"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeP:
    def __init__(self, pid, ppid):
        self.pid_ = pid
        self._ppid = ppid
        self.killed = []

    def name(self):
        return "QtWebEngineProcess.exe"

    def ppid(self):
        return self._ppid

    def terminate(self):
        self.killed.append("t")


def test_active_child_of_current_process_not_killed(monkeypatch):
    """当前进程的存活 WebEngine 后代（正在渲染）绝不被杀"""
    scanner = _load_scanner()
    monkeypatch.setattr(sys, "platform", "win32")  # 锁定 Windows 枚举路径（mac 走 pgrep）

    active_child = _FakeP(700, os.getpid())  # 活后代：正在渲染消息
    orphan = _FakeP(800, 9999)  # 孤儿：父进程 9999 已消失

    fake_csv = (
        '"QtWebEngineProcess.exe","700","Console","1","1,000 K"\r\n'
        '"QtWebEngineProcess.exe","800","Console","1","1,000 K"\r\n'
    ).encode("utf-8")

    import psutil

    monkeypatch.setattr(
        psutil,
        "pid_exists",
        lambda pid: pid == os.getpid(),  # 只有当前进程存在
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: {700: active_child, 800: orphan}[pid])
    monkeypatch.setattr(psutil, "wait_procs", lambda procs_, timeout=None: (procs_, []))
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, fake_csv, b"")
    )

    killed = scanner._kill_qwebengine_processes()
    assert killed == 1, f"只应终止 1 个孤儿（800），实际 {killed}"
    assert active_child.killed == [], "活后代（在途渲染）被误杀"
    assert orphan.killed == ["t"], "真孤儿应被清理"


def test_active_child_hidden_from_tasklist_not_killed(monkeypatch):
    """活后代即使 tasklist 快照缺失（旧实现经 children() 误杀的路径）也不被杀"""
    scanner = _load_scanner()
    monkeypatch.setattr(sys, "platform", "win32")

    active_child = _FakeP(700, os.getpid())
    fake_csv = b'"System","4","Services"\r\n'

    import psutil

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == os.getpid())
    monkeypatch.setattr(psutil, "wait_procs", lambda procs_, timeout=None: (procs_, []))
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, fake_csv, b"")
    )
    # 旧实现会走 children(recursive=True) 枚举到活后代；桩记录调用以暴露该路径
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda pid: types.SimpleNamespace(children=lambda recursive=True: [active_child]),
    )

    killed = scanner._kill_qwebengine_processes()
    assert killed == 0
    assert active_child.killed == [], "children() 枚举路径仍在误杀活后代"
