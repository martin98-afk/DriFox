# -*- coding: utf-8 -*-
"""原生崩溃捕获（faulthandler）与崩溃报告检测

Python 层异常有 sys.excepthook / sys.unraisablehook 兜底（main.py），
但 Qt/C++ 层的段错误不经过 Python——打包版表现为「闪退且 all.log 无任何
记录」。本模块用 faulthandler 在致命信号（SIGSEGV/SIGILL/SIGFPE/SIGABRT...）
触发时把原生 + Python 混合调用栈 dump 到 logs/crash/，下次启动检测残留
dump 并弹窗告知报告位置，解决「闪退后无从排查」的问题。

判定规则：
- dump 文件非空且无 clean-exit 标记 → 发生过原生崩溃，弹窗报告
- 含 clean-exit 标记 → 正常退出，静默清理
- 空文件（taskkill 强杀/断电，faulthandler 未触发）→ 静默清理，不误报

已弹窗确认的报告重命名为 *.log.reported（保留取证，不再弹窗）。
"""
import atexit
import os
import sys
import time
from pathlib import Path
from typing import Optional

_CLEAN_EXIT_MARK = "=== clean exit ==="

# WER 报告根目录（ReportQueue/ReportArchive 存 AppCrash_<exe> 崩溃报告），
# 模块常量便于测试 monkeypatch 重定向
_WER_REPORT_BASE = Path(r"C:\ProgramData\Microsoft\Windows\WER")

# 模块级持有文件句柄：faulthandler 要求 dump 期间 fd 存活，
# 句柄被 GC 关闭后崩溃时将无法写入
_crash_file = None


def install_crash_handler(logs_dir: Path) -> Optional[Path]:
    """启用 faulthandler，崩溃时调用栈 dump 到 logs_dir/crash/。

    返回 dump 文件路径；启用失败返回 None（绝不阻塞启动）。
    在 _deferred_startup 中调用（日志目录就绪后）。
    """
    global _crash_file
    try:
        import faulthandler

        crash_dir = Path(logs_dir) / "crash"
        crash_dir.mkdir(parents=True, exist_ok=True)
        dump_path = crash_dir / f"crash_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"
        _crash_file = open(dump_path, "w", encoding="utf-8")
        faulthandler.enable(file=_crash_file)
        atexit.register(_mark_clean_exit)
        _setup_wer_localdumps(crash_dir)
        return dump_path
    except Exception:
        return None


def _setup_wer_localdumps(crash_dir: Path) -> Optional[Path]:
    """配置 WER LocalDumps（HKLM）：原生崩溃时系统自动写完整 minidump。

    faulthandler 在 Windows 拿不到 C 栈（CPython 实现依赖 glibc backtrace(3)，
    Windows 无此 API），WER 的 .dmp 由系统 DbgHelp 生成，含 C 栈/寄存器/模块
    列表。分析：``cdb -z xxx.dmp -c "!analyze -v;q"``。

    LocalDumps 只认 HKLM（HKCU 不生效，已实测），写 HKLM 需管理员权限：
    打包运行期通常无权限 → 失败静默，降级靠 WER 默认报告（Report.wer，
    见 _nearby_wer_report）；安装器提权场景可成功写入。
    返回 dumps 目录；未配置返回 None。
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    try:
        import winreg

        dumps_dir = Path(crash_dir) / "dumps"
        dumps_dir.mkdir(parents=True, exist_ok=True)
        app_exe = Path(sys.executable).name  # PyInstaller 产物名，如 Drifox.exe
        key_path = rf"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\{app_exe}"
        with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            winreg.SetValueEx(key, "DumpFolder", 0, winreg.REG_EXPAND_SZ, str(dumps_dir))
            winreg.SetValueEx(key, "DumpType", 0, winreg.REG_DWORD, 1)  # 1=minidump
            winreg.SetValueEx(key, "DumpCount", 0, winreg.REG_DWORD, 5)
        return dumps_dir
    except Exception:
        return None


def _mark_clean_exit() -> None:
    """正常退出时打标记，检测逻辑据此区分「崩过」与「正常关闭」。"""
    try:
        if _crash_file is not None and not _crash_file.closed:
            _crash_file.write(_CLEAN_EXIT_MARK + "\n")
            _crash_file.close()
    except Exception:
        pass


def check_last_crash(logs_dir: Path) -> Optional[Path]:
    """扫描 crash 目录，返回待报告的最新崩溃 dump 路径（无则 None）。

    已确认非崩溃的文件（空文件/含 clean-exit 标记）就地清理。
    """
    try:
        crash_dir = Path(logs_dir) / "crash"
        if not crash_dir.is_dir():
            return None
        latest: Optional[Path] = None
        for f in crash_dir.glob("crash_*.log"):
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if content.strip() and _CLEAN_EXIT_MARK not in content:
                if latest is None or f.stat().st_mtime > latest.stat().st_mtime:
                    latest = f
            else:
                _silent_remove(f)
        return latest
    except Exception:
        return None


def _nearby_wer_dump(crash_log: Path, window_s: float = 600.0) -> Optional[str]:
    """找与崩溃 log 同窗口期（默认 ±10 分钟）的 WER minidump。

    返回提示文案；无关联 dump 返回 None。WER dump 不自动删除：
    含完整 C 栈，是使用者需要回传给开发者的核心取证文件。
    """
    try:
        crash_log = Path(crash_log)
        dumps = list((crash_log.parent / "dumps").glob("*.dmp"))
        if not dumps:
            return None
        latest = max(dumps, key=lambda p: p.stat().st_mtime)
        if abs(latest.stat().st_mtime - crash_log.stat().st_mtime) > window_s:
            return None
        ts = time.strftime("%H:%M:%S", time.localtime(latest.stat().st_mtime))
        return f"已生成系统级内存转储（含 C 栈，请一并回传）：{latest.name}（{ts}）"
    except Exception:
        return None


def _nearby_wer_report(crash_log: Path, window_s: float = 600.0) -> Optional[str]:
    """扫描系统 WER 报告目录，找同窗口期本应用的崩溃报告（Report.wer）。

    WER 默认行为：崩溃后在 ReportQueue/ReportArchive 留 Report.wer
    （UTF-16 文本，含崩溃模块签名如 P1=exe、P4=Qt5Core.dll、P7=异常码），
    .dmp 则默认不保留。此路径零权限、零配置，是对 LocalDumps（需管理员
    写 HKLM）失败时的兜底取证。返回提示文案；无关联报告返回 None。
    """
    if sys.platform != "win32":
        return None
    try:
        crash_log = Path(crash_log)
        exe_name = Path(sys.executable).stem
        best = None
        for sub in ("ReportQueue", "ReportArchive"):
            for rep in (_WER_REPORT_BASE / sub).glob(f"AppCrash_{exe_name}_*"):
                wer = rep / "Report.wer"
                if not wer.is_file():
                    continue
                mtime = wer.stat().st_mtime
                if abs(mtime - crash_log.stat().st_mtime) > window_s:
                    continue
                if best is None or mtime > best.stat().st_mtime:
                    best = wer
        if best is None:
            return None
        ts = time.strftime("%H:%M:%S", time.localtime(best.stat().st_mtime))
        return f"系统崩溃报告（含崩溃模块签名，请一并回传）：{best.parent}（{ts}）"
    except Exception:
        return None


def prompt_crash_report(dump_path: Path, parent=None) -> None:
    """弹窗展示上次崩溃摘要，并提供打开报告目录的入口。

    弹窗关闭后删除 dump，避免下次启动重复报告。
    """
    try:
        from PyQt5.QtCore import QUrl
        from PyQt5.QtGui import QDesktopServices
        from PyQt5.QtWidgets import QMessageBox

        dump_path = Path(dump_path)
        try:
            crash_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(dump_path.stat().st_mtime))
        except Exception:
            crash_ts = "未知时间"
        try:
            lines = dump_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            lines = []
        excerpt = "\n".join(lines[:12])
        if len(lines) > 12:
            excerpt += "\n..."

        # 关联同一次崩溃的系统级取证（±10 分钟窗口）
        wer_note = _nearby_wer_dump(dump_path) or _nearby_wer_report(dump_path)
        if wer_note:
            excerpt += f"\n\n{wer_note}"

        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("检测到上次异常退出")
        box.setText(f"上次运行发生了原生崩溃（闪退），崩溃时间：{crash_ts}")
        box.setInformativeText(f"崩溃报告：{dump_path}\n\n{excerpt or '（报告内容为空）'}")
        open_btn = box.addButton("打开报告目录", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(dump_path.parent)))
        # 报告已告知 → 重命名标记已读：文件保留供排查（崩溃证据不可再生），
        # 后缀变化使 check_last_crash 不再命中，避免下次启动重复弹窗
        try:
            dump_path.rename(dump_path.with_name(dump_path.name + ".reported"))
        except Exception:
            pass
    except Exception:
        pass


def _silent_remove(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass
