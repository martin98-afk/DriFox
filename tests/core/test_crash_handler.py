# -*- coding: utf-8 -*-
"""crash_handler 单元测试：崩溃 dump 检测与清理规则

背景：打包版原生崩溃（Qt/C++ 段错误）不经过 Python excepthook，
表现为「闪退且 all.log 无记录」。crash_handler 用 faulthandler 落盘
dump，本文件覆盖下次启动的检测/清理/报告判定逻辑。
"""
import os
import time
from pathlib import Path

from app.core.crash_handler import (
    _CLEAN_EXIT_MARK,
    _mark_clean_exit,
    _nearby_wer_dump,
    _nearby_wer_report,
    _setup_wer_localdumps,
    check_pending_crashes,
    install_crash_handler,
)


def _make_dump(crash_dir: Path, name: str, content: str) -> Path:
    crash_dir.mkdir(parents=True, exist_ok=True)
    f = crash_dir / name
    f.write_text(content, encoding="utf-8")
    return f


def test_check_no_dir(tmp_path):
    assert check_pending_crashes(tmp_path / "logs") == []


def test_check_empty_dir(tmp_path):
    (tmp_path / "logs" / "crash").mkdir(parents=True)
    assert check_pending_crashes(tmp_path / "logs") == []


def test_crash_dump_reported(tmp_path):
    logs = tmp_path / "logs"
    dump = _make_dump(logs / "crash", "crash_1.log", "Fatal Python error: Segmentation fault\nstack...")
    assert check_pending_crashes(logs) == [dump]
    # 崩溃 dump 不被清理（弹窗确认后才删）
    assert dump.exists()


def test_reported_suffix_not_matched(tmp_path):
    """已报告（.reported 后缀）的 dump 不再命中，每条只弹一次。"""
    logs = tmp_path / "logs"
    _make_dump(logs / "crash", "crash_1.log.reported", "crash already shown")
    assert check_pending_crashes(logs) == []


def test_clean_exit_dump_cleared(tmp_path):
    logs = tmp_path / "logs"
    f = _make_dump(logs / "crash", "crash_1.log", f"stack...\n{_CLEAN_EXIT_MARK}\n")
    assert check_pending_crashes(logs) == []
    assert not f.exists()


def test_empty_dump_cleared_not_reported(tmp_path):
    """空文件 = taskkill 强杀/断电（faulthandler 未触发），静默清理不误报。"""
    logs = tmp_path / "logs"
    f = _make_dump(logs / "crash", "crash_1.log", "")
    assert check_pending_crashes(logs) == []
    assert not f.exists()


def test_all_pending_returned_oldest_first(tmp_path):
    """积压多份未报告 dump 全部返回，按崩溃时间从旧到新逐条弹。"""
    logs = tmp_path / "logs"
    old = _make_dump(logs / "crash", "crash_100.log", "crash old")
    new = _make_dump(logs / "crash", "crash_200.log", "crash new")
    os.utime(old, (time.time() - 10, time.time() - 10))
    assert check_pending_crashes(logs) == [old, new]


def test_install_and_clean_exit(tmp_path):
    logs = tmp_path / "logs"
    dump = install_crash_handler(logs)
    assert dump is not None and dump.exists()
    assert (logs / "crash").is_dir()
    _mark_clean_exit()
    assert _CLEAN_EXIT_MARK in dump.read_text(encoding="utf-8")
    # 正常退出后不应报告
    assert check_pending_crashes(logs) == []


# ========== WER LocalDumps ==========


class _FakeWinReg:
    """winreg 替身：记录 SetValueEx 调用，不触碰真实注册表。"""

    HKEY_LOCAL_MACHINE = "HKLM"
    REG_EXPAND_SZ = 2
    REG_DWORD = 4

    def __init__(self):
        self.calls = []

    def CreateKey(self, root, path):
        return _FakeKey(path)

    def SetValueEx(self, key, name, reserved, typ, value):
        self.calls.append((key._path, name, typ, value))


class _FakeKey:
    def __init__(self, path):
        self._path = path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_wer_skipped_in_dev(tmp_path):
    """dev 环境（非 frozen）不写注册表、不建 dumps 目录。"""
    assert _setup_wer_localdumps(tmp_path) is None


def test_wer_configures_registry(tmp_path, monkeypatch):
    """打包环境：HKLM 注册表写入 DumpFolder/DumpType/DumpCount。"""
    import sys as _sys

    fake = _FakeWinReg()
    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)
    crash_dir = tmp_path / "crash"
    result = _setup_wer_localdumps(crash_dir)
    assert result == crash_dir / "dumps"
    assert result.is_dir()
    paths = [c[0] for c in fake.calls]
    assert all("LocalDumps" in p for p in paths)
    values = {c[1]: (c[2], c[3]) for c in fake.calls}
    assert values["DumpType"] == (_FakeWinReg.REG_DWORD, 1)
    assert values["DumpCount"] == (_FakeWinReg.REG_DWORD, 5)
    assert values["DumpFolder"][1] == str(crash_dir / "dumps")


def test_nearby_wer_dump_window(tmp_path):
    """±10 分钟外的 WER dump 不关联，不提示。"""
    import os
    import time as _time

    crash_dir = tmp_path / "crash"
    crash_dir.mkdir(parents=True)
    log = crash_dir / "crash_1.log"
    log.write_text("segfault", encoding="utf-8")
    # 无 dump → None
    assert _nearby_wer_dump(log) is None
    # 同窗口期 → 提示
    dumps_dir = crash_dir / "dumps"
    dumps_dir.mkdir()
    dmp = dumps_dir / "Drifox.exe.1234.dmp"
    dmp.write_bytes(b"MDMP")
    assert _nearby_wer_dump(log) is not None
    # dump 与崩溃不同期（相差 >10 分钟）→ 不关联
    old = _time.time() - 3600
    os.utime(dmp, (old, old))
    assert _nearby_wer_dump(log) is None


def test_nearby_wer_report(tmp_path, monkeypatch):
    """WER ReportQueue/ReportArchive 里同窗口期 AppCrash_<exe> 报告可检出。"""
    import os
    import sys as _sys
    import time as _time

    exe_name = Path(_sys.executable).stem
    wer_base = tmp_path / "WER"
    monkeypatch.setattr("app.core.crash_handler._WER_REPORT_BASE", wer_base)

    crash_dir = tmp_path / "crash"
    crash_dir.mkdir(parents=True)
    log = crash_dir / "crash_1.log"
    log.write_text("segfault", encoding="utf-8")

    # 无报告 → None
    assert _nearby_wer_report(log) is None

    # 同窗口期报告 → 提示
    rep_dir = wer_base / "ReportArchive" / f"AppCrash_{exe_name}_abc123"
    rep_dir.mkdir(parents=True)
    (rep_dir / "Report.wer").write_text("sig", encoding="utf-16")
    note = _nearby_wer_report(log)
    assert note is not None and str(rep_dir) in note

    # 超窗口 → None
    old = _time.time() - 3600
    os.utime(rep_dir / "Report.wer", (old, old))
    assert _nearby_wer_report(log) is None
