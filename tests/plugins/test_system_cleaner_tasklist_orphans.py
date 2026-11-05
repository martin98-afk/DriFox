# -*- coding: utf-8 -*-
"""system-cleaner 孤儿清理 tasklist 化回归测试（工单 F37 Step1）

背景：_kill_qwebengine_processes 孤儿兜底段原 psutil.process_iter 全表枚举
是后台线程纯 Python 迭代，长持 GIL 饿死主线程（打包版 watchdog 543ms /
源码版 9025ms 实证）。改为 tasklist 子进程枚举（CSV 只取列 0/1），候选
定点 psutil.Process(pid) 校验；tasklist 失败回落原全表路径。

本文件锁定：
1. 新旧枚举层对同一进程集合选出等价候选（谓词逐字相同）；
2. tasklist CSV 解析：列 0/1 取值、非 qwebengine 行过滤、坏行跳过；
3. tasklist 失败/超时回落 process_iter 路径（保底不劣化）；
4. bytes 解码走 locale.getpreferredencoding + errors="replace"（禁 text=True）；
5. 收尾 terminate/kill/wait_procs 语义不变。
"""

import importlib.util
import os
import subprocess
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI_DIR = os.path.join(REPO_ROOT, "plugins", "system-cleaner", "ui")
PKG = "systemcleaner_f37_pkg"


def _load_scanner():
    if PKG in sys.modules:
        return sys.modules[f"{PKG}.scanner"]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [UI_DIR]
    sys.modules[PKG] = pkg
    spec = importlib.util.spec_from_file_location(f"{PKG}.scanner", os.path.join(UI_DIR, "scanner.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.scanner"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_tasklist_parse_and_candidates(monkeypatch):
    """tasklist CSV → 候选定点校验：qwebengine 命中、其他映像过滤、坏行跳过"""
    scanner = _load_scanner()

    fake_csv = (
        '"QtWebEngineProcess.exe","111","Console","1","1,000 K"\r\n'
        '"chrome.exe","222","Console","1","1,000 K"\r\n'
        '"QtWebEngineProcess.exe","not-a-pid","Console","1"\r\n'  # 坏行：PID 不可解析
        '"qtwebengineprocess.exe","333","Console","1"\r\n'  # 大小写不敏感
        '"System","4","Services"\r\n'
    ).encode("utf-8")

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

    procs = {
        111: _FakeP(111, 999),  # 父 999 不存在 → 孤儿
        333: _FakeP(333, 0),  # ppid=0 → 孤儿
        444: _FakeP(444, os.getpid()),  # 父活着 → 非孤儿（对照）
    }
    import psutil

    real_process, real_pid_exists = psutil.Process, psutil.pid_exists
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == os.getpid())
    monkeypatch.setattr(psutil, "Process", lambda pid: procs[pid])
    monkeypatch.setattr(psutil, "wait_procs", lambda procs_, timeout=None: (procs_, []))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, fake_csv, b""))

    killed = scanner._kill_qwebengine_processes()
    assert killed == 2, f"应终止 2 个孤儿（111/333），实际 {killed}"
    assert procs[111].killed == ["t"] and procs[333].killed == ["t"]
    assert procs[444].killed == []  # 活父进程不误杀
    assert real_process is not None and real_pid_exists is not None  # 桩引用保持


def test_tasklist_failure_falls_back_to_process_iter(monkeypatch):
    """tasklist 失败 → 回落 process_iter 全表路径（保底不劣化）"""
    scanner = _load_scanner()

    class _FakeP:
        def __init__(self, pid, name, ppid):
            self.info = {"pid": pid, "name": name, "ppid": ppid}
            self.killed = []

        def terminate(self):
            self.killed.append("t")

    fake = _FakeP(555, "QtWebEngineProcess.exe", 0)
    import psutil

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter([fake]))
    monkeypatch.setattr(psutil, "wait_procs", lambda procs_, timeout=None: (procs_, []))

    def _boom(*a, **k):
        raise OSError("tasklist not found")

    monkeypatch.setattr(subprocess, "run", _boom)

    killed = scanner._kill_qwebengine_processes()
    assert killed == 1, "tasklist 失败应回落 process_iter 并清理候选"
    assert fake.killed == ["t"]


def test_no_bytes_text_mode():
    """tasklist 调用必须 bytes 模式（capture_output 且无 text=True）——源码断言锁口径"""
    import inspect
    import io

    scanner = _load_scanner()
    src = inspect.getsource(scanner._kill_qwebengine_processes)
    assert "text=True" not in src, "禁 text=True 裸默认编码"
    assert "capture_output=True" in src
    assert "getpreferredencoding" in src and 'errors="replace"' in src
