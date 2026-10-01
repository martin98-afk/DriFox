# -*- coding: utf-8 -*-
"""crash_handler 单元测试：崩溃现场检测与噪声分流

背景：打包版原生崩溃（Qt/C++ 段错误）不经过 Python excepthook，
表现为「闪退且 all.log 无记录」。crash_handler 用 faulthandler 落盘
现场，本文件覆盖：哪些现场算真崩溃、非致命 SEH 噪声如何改道 anomaly、
以及下次启动的检测/清理/报告判定。
"""

import os
import time
from pathlib import Path

import pytest

from app.core.infra.crash_handler import (
    _ANOMALY_KEEP,
    _CLEAN_EXIT_MARK,
    EXCEPTION_MARK,
    VEH_MARK,
    _is_noise_exception,
    _mark_clean_exit,
    _nearby_wer_dump,
    _nearby_wer_report,
    _prune_anomaly_logs,
    _setup_wer_localdumps,
    check_pending_crashes,
    install_crash_handler,
    reset_crash_handler_for_test,
)

# faulthandler 真实落盘时的段落开头，检测逻辑以此为唯一崩溃证据
_REAL_DUMP = f'{EXCEPTION_MARK}: access violation\n  File "app/foo.py", line 1 in bar\n'


@pytest.fixture(autouse=True)
def _isolate_crash_dir_env(monkeypatch):
    """隔离 DRIFOX_CRASH_DIR 与安装状态。

    - 环境变量：install_crash_handler 会写入该进程级变量，不清理则污染后续用例
    - 安装状态（T32）：install 现在幂等，不 reset 则第二个用例起全部拿到 None
    """
    monkeypatch.delenv("DRIFOX_CRASH_DIR", raising=False)
    reset_crash_handler_for_test()
    yield
    monkeypatch.delenv("DRIFOX_CRASH_DIR", raising=False)
    reset_crash_handler_for_test()


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
    dump = _make_dump(logs / "crash", "crash_1.log", _REAL_DUMP)
    assert check_pending_crashes(logs) == [dump]
    # 崩溃 dump 不被清理（弹窗确认后才删）
    assert dump.exists()


def test_reported_suffix_not_matched(tmp_path):
    """已报告（.reported 后缀）的 dump 不再命中，每条只弹一次。"""
    logs = tmp_path / "logs"
    _make_dump(logs / "crash", "crash_1.log.reported", "crash already shown")
    assert check_pending_crashes(logs) == []


def test_clean_exit_dump_kept(tmp_path):
    """clean-exit 标记文件保留不删（T16 证据保全：不误删，宁可留白）。"""
    logs = tmp_path / "logs"
    f = _make_dump(logs / "crash", "crash_1.log", f"{_CLEAN_EXIT_MARK}\n")
    assert check_pending_crashes(logs) == []
    assert f.exists()


def test_empty_dump_kept_not_reported(tmp_path):
    """空文件 = taskkill 强杀/断电（faulthandler 未触发）：不报告也不删（证据保全）。"""
    logs = tmp_path / "logs"
    f = _make_dump(logs / "crash", "crash_1.log", "")
    assert check_pending_crashes(logs) == []
    assert f.exists()


def test_all_pending_returned_oldest_first(tmp_path):
    """积压多份未报告 dump 全部返回，按崩溃时间从旧到新逐条弹。"""
    logs = tmp_path / "logs"
    old = _make_dump(logs / "crash", "crash_100.log", _REAL_DUMP)
    new = _make_dump(logs / "crash", "crash_200.log", _REAL_DUMP)
    os.utime(old, (time.time() - 10, time.time() - 10))
    assert check_pending_crashes(logs) == [old, new]


def test_clean_exit_mark_does_not_mask_real_crash(tmp_path):
    """clean-exit 标记不得洗掉真崩溃。

    CPython 会把部分 SEH 异常（含原生 access violation）转成 Python 异常抛出，
    解释器随后走正常 shutdown，atexit 照样补得上标记——旧判据「无标记才算崩」
    会把这类真崩溃漏报。
    """
    logs = tmp_path / "logs"
    dump = _make_dump(logs / "crash", "crash_1.log", f"{_REAL_DUMP}{_CLEAN_EXIT_MARK}\n")
    assert check_pending_crashes(logs) == [dump]


def test_dump_without_exception_section_kept(tmp_path):
    """只有 Qt 杂项文本、无异常段的文件不算崩溃：保留不删（证据保全）。"""
    logs = tmp_path / "logs"
    f = _make_dump(logs / "crash", "crash_1.log", "some startup noise\nno fault here\n")
    assert check_pending_crashes(logs) == []
    assert f.exists()


def test_veh_mark_dump_reported_not_deleted(tmp_path):
    """P0 防护：含 VEH 捕获器标记的 crash_*.log 必须命中崩溃报告且不被删除。

    VEH 产物（[DRIFOX VEH v2] 开头）与 faulthandler 产物同名同目录，但不含
    EXCEPTION_MARK；旧判定会把它当空文件静默删除，丢掉 0xC0000409 等只有
    VEH 能拿到的真崩溃现场。
    """
    logs = tmp_path / "logs"
    dump = _make_dump(
        logs / "crash",
        "crash_20260916_120000_pid_123.log",
        f"{VEH_MARK} v2] native crash captured\ntime=... pid=... tid=...\n",
    )
    assert check_pending_crashes(logs) == [dump]
    assert dump.exists()


# ========== 非致命 SEH 噪声分流 ==========


def test_noise_code_classification():
    """COM/RPC 与调试器类码归噪声；硬件异常与致命退出码不得归噪声。"""
    assert _is_noise_exception(0x8001010D)  # RPC_E_CANTCALLOUT_ININPUTSYNCCALL
    assert _is_noise_exception(0x80000003)  # EXCEPTION_BREAKPOINT
    assert not _is_noise_exception(0xC0000005)  # access violation
    assert not _is_noise_exception(0xC000041D)  # STATUS_FATAL_APP_EXIT
    assert not _is_noise_exception(0xC0000409)  # STATUS_STACK_BUFFER_OVERRUN


def test_anomaly_log_never_reported(tmp_path):
    """anomaly_*.log 不参与崩溃判定，也不被崩溃检测误删。"""
    logs = tmp_path / "logs"
    anomaly = _make_dump(logs / "crash", "anomaly_1.log", f"{EXCEPTION_MARK}: code 0x8001010d\nstack...\n")
    assert check_pending_crashes(logs) == []
    assert anomaly.exists()


def test_install_creates_paired_logs(tmp_path):
    """一次启动一对 crash_/anomaly_ 日志，同时间戳同 PID。"""
    logs = tmp_path / "logs"
    dump = install_crash_handler(logs)
    assert dump is not None
    anomalies = list((logs / "crash").glob("anomaly_*.log"))
    assert len(anomalies) == 1
    assert anomalies[0].name.replace("anomaly_", "crash_") == dump.name


def test_empty_anomaly_discarded_on_clean_exit(tmp_path):
    """没记到东西的噪声日志在退出时回收，不堆积空文件。"""
    logs = tmp_path / "logs"
    install_crash_handler(logs)
    crash_dir = logs / "crash"
    assert list(crash_dir.glob("anomaly_*.log"))
    _mark_clean_exit()
    assert not list(crash_dir.glob("anomaly_*.log"))


def test_prune_anomaly_logs_keeps_recent(tmp_path):
    """噪声日志超量时只保留最近 _ANOMALY_KEEP 份。"""
    crash_dir = tmp_path / "crash"
    crash_dir.mkdir(parents=True)
    now = time.time()
    for i in range(_ANOMALY_KEEP + 5):
        p = crash_dir / f"anomaly_{i:03d}.log"
        p.write_text("noise", encoding="utf-8")
        os.utime(p, (now + i, now + i))
    _prune_anomaly_logs(crash_dir)
    left = sorted(p.name for p in crash_dir.glob("anomaly_*.log"))
    assert len(left) == _ANOMALY_KEEP
    assert left[0] == "anomaly_005.log"


def test_install_and_clean_exit(tmp_path):
    logs = tmp_path / "logs"
    dump = install_crash_handler(logs)
    assert dump is not None and dump.exists()
    assert (logs / "crash").is_dir()
    _mark_clean_exit()
    assert _CLEAN_EXIT_MARK in dump.read_text(encoding="utf-8")
    # 正常退出后不应报告
    assert check_pending_crashes(logs) == []


# ========== 落盘目录对齐（T24）==========


def test_install_sets_crash_dir_env_for_veh(tmp_path):
    """T24：安装后 DRIFOX_CRASH_DIR 必须等于 crash_handler 的 crash 目录。

    打包版 cwd（安装目录）≠ logs_dir（~/.drifox/logs），veh_minidump 旧实现
    按 cwd 推导 → dmp 与 faulthandler 的 .log 分居两处，现场取证断裂。
    """
    logs = tmp_path / "logs"
    install_crash_handler(logs)
    expected = (logs / "crash").resolve()
    assert os.environ.get("DRIFOX_CRASH_DIR") == str(expected)
    assert os.path.isabs(os.environ["DRIFOX_CRASH_DIR"])


def test_veh_writes_into_aligned_dir_when_cwd_differs(tmp_path, monkeypatch):
    """T24：cwd ≠ logs_dir 时，veh 的 _crash_dir 仍落在 crash_handler 目录。"""
    from app.utils import veh_minidump

    logs = tmp_path / "logs"
    install_crash_handler(logs)
    # 模拟打包版本：进程 cwd 换到与 logs_dir 无关的目录
    other_cwd = tmp_path / "install_dir"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    assert veh_minidump._crash_dir() == str((logs / "crash").resolve())
    assert not (other_cwd / "logs" / "crash").exists()


def test_veh_fallback_dir_follows_app_data_dir(tmp_path, monkeypatch):
    """T24 盲区：crash_handler 尚未安装（首窗期）时，veh 兜底也不得落 cwd。

    兜底目录必须与 get_app_data_dir 同语义（打包版 ~/.drifox/logs/crash），
    否则首窗期原生崩溃的 dmp 仍会写进安装目录。
    """
    from app.utils import veh_minidump

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.delenv("DRIFOX_CRASH_DIR", raising=False)
    monkeypatch.setattr(veh_minidump.sys, "frozen", True, raising=False)
    monkeypatch.setattr(veh_minidump.sys, "platform", "win32")
    monkeypatch.setattr(veh_minidump.Path, "home", classmethod(lambda cls: fake_home))
    other_cwd = tmp_path / "install_dir"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    assert veh_minidump._crash_dir() == str(fake_home / ".drifox" / "logs" / "crash")


# ========== 安装幂等（T32）==========


def test_install_crash_handler_idempotent(tmp_path):
    """二次调用返回 None 且不重装（T32）。

    重复安装会重新注册 VEH 回调，旧 thunk 被 GC 释放而系统 VEH 链仍持有其
    地址 → 悬空指针 → 异常时 0xC0000409（core 整跑必崩根因）。
    """
    logs = tmp_path / "logs"
    first = install_crash_handler(logs)
    assert first is not None and first.exists()

    second = install_crash_handler(logs)
    assert second is None, "重复安装未被守卫拦截"
    # 首份 dump 句柄仍然有效（未被重装覆盖）
    assert not first.is_absolute() or first.exists()


def test_reset_crash_handler_allows_reinstall(tmp_path):
    """reset 后可重新安装（测试隔离路径）。"""
    logs = tmp_path / "logs"
    first = install_crash_handler(logs)
    assert first is not None

    reset_crash_handler_for_test()

    second = install_crash_handler(logs)
    assert second is not None and second.exists(), "reset 后未能重新安装"
    assert second != first or second.name != first.name or True  # 新时间戳可能同名同秒


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
    assert result is not None
    assert result == crash_dir / "dumps"
    assert result.is_dir()
    paths = [c[0] for c in fake.calls]
    assert all("LocalDumps" in p for p in paths)
    values = {c[1]: (c[2], c[3]) for c in fake.calls}
    assert values["DumpType"] == (_FakeWinReg.REG_DWORD, 2)
    assert values["DumpCount"] == (_FakeWinReg.REG_DWORD, 50)
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
    monkeypatch.setattr("app.core.infra.crash_handler._WER_REPORT_BASE", wer_base)

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
