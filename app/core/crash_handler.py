# -*- coding: utf-8 -*-
"""原生崩溃捕获（faulthandler）与崩溃报告检测

Python 层异常有 sys.excepthook / sys.unraisablehook 兜底（main.py），
但 Qt/C++ 层的段错误不经过 Python——打包版表现为「闪退且 all.log 无任何
记录」。本模块用 faulthandler 在致命信号（SIGSEGV/SIGILL/SIGFPE/SIGABRT...）
触发时把原生 + Python 混合调用栈 dump 到 logs/crash/，下次启动检测残留
dump 并以 InfoBar 横幅告知报告位置，解决「闪退后无从排查」的问题。

判定规则：
- dump 文件含「Windows fatal exception」（faulthandler）或「[DRIFOX VEH」（VEH
  捕获器）现场标记 → 发生过原生崩溃，InfoBar 报告
- 未命中标记的文件一律保留（证据保全，不误删）——空文件也可能来自取证链
  尚未覆盖的崩溃形态

已提示确认的报告重命名为 *.log.reported（保留取证，不再提示）。

噪声分流：faulthandler 在 Windows 上注册的 VEH 无差别记录所有 SEH 异常，
其中 COM/RPC 与调试器类异常会被上层处理器消化、进程照常存活，混进崩溃
文件会造成「上次异常退出」假警报。本模块用一层自建 VEH 按异常码把这些
现场改道到 anomaly_*.log（不参与崩溃判定），详见 _install_seh_classifier。
"""

import atexit
import os
import sys
import time
from pathlib import Path
from typing import Optional

from loguru import logger

_CLEAN_EXIT_MARK = "=== clean exit ==="

# faulthandler 落盘现场的固定开头，用作「发生过 SEH 异常」的唯一可箱证据
EXCEPTION_MARK = "Windows fatal exception"

# VEH 捕获器（app/utils/veh_minidump.py）落盘现场的开头标记。与 faulthandler
# 产物同为 crash_*.log 命名模式，但不含 EXCEPTION_MARK；缺此判定时 VEH 记录的
# 真崩溃（0xC0000409 等 faulthandler 拿不到的异常）会被误当空文件删除。
VEH_MARK = "[DRIFOX VEH"

# WER 报告根目录（ReportQueue/ReportArchive 存 AppCrash_<exe> 崩溃报告），
# 模块常量便于测试 monkeypatch 重定向
_WER_REPORT_BASE = Path(r"C:\ProgramData\Microsoft\Windows\WER")

# 模块级持有文件句柄：faulthandler 要求 dump 期间 fd 存活，
# 句柄被 GC 关闭后崩溃时将无法写入
_crash_file = None

# 非致命异常（噪声）现场落盘的文件句柄，与 _crash_file 同生命周期
_noise_file = None

# 取证产物目录（qt 消息日志），模块级供回调链使用
_forensic_dir: Optional[Path] = None

# VEH 回调必须模块级持有，防 GC 后野指针
_veh_handler_ref = None

# 安装幂等守卫（T32）：install_crash_handler 可能被多次调用（测试里 reset 单例
# 后重装、deferred_startup 重入等）。重复安装会重新注册 VEH 回调并让上一份
# cb（WINFUNCTYPE thunk）失去引用 → GC 释放 thunk 内存，而 VEH 链里仍留着
# 它的地址 → 异常发生时跳到已释放内存 → 0xC0000409 fastfail（core 整跑必崩
# 的根因）。置 True 后二次调用直接返回，不再重复注册。
_crash_handler_installed = False

# 噪声异常码：均为「上层处理器能消化、进程不会因此终止」的 SEH 码。
# 采黑名单 + 默认可疑策略：未列入的一律当崩溃写 crash 文件，宁可噪声
# 漏进崩溃文件，也不能丢掉真实崩溃现场。
_NOISE_EXCEPTION_CODES = frozenset(
    {
        0x8001010D,  # RPC_E_CANTCALLOUT_ININPUTSYNCCALL 输入同步期发起 COM 外呼
        0x80010001,  # RPC_E_SERVERCALL_RETRYLATER       被调用方忙，重试即可
        0x8001010A,  # RPC_E_CALL_REJECTED               调用被拒，重试即可
        0x80000003,  # EXCEPTION_BREAKPOINT              调试断点 / Chromium DCHECK
        0x40010006,  # OUTPUT_DEBUG_STRING               调试器调试输出
        0x406D1388,  # MSVC 线程命名约定，仅用于通知调试器
        0xE0434352,  # CLR 托管异常（.NET 互操作层）
    }
)

# anomaly_*.log 保留份数
_ANOMALY_KEEP = 20


def install_crash_handler(logs_dir: Path) -> Optional[Path]:
    """启用 faulthandler，崩溃时调用栈 dump 到 logs_dir/crash/。

    返回 dump 文件路径；启用失败返回 None（绝不阻塞启动）。
    在 _deferred_startup 中调用（日志目录就绪后）。

    幂等：重复调用直接返回 None（T32）。重复安装会重复注册 VEH 回调并让
    上一份回调 thunk 被 GC 释放，而 VEH 链仍持有其地址 → 悬空指针 →
    异常发生时 0xC0000409 fastfail（core 整跑必崩的根因）。
    """
    global _crash_file, _noise_file, _crash_handler_installed
    if _crash_handler_installed:
        return None
    try:
        import faulthandler

        crash_dir = Path(logs_dir).resolve() / "crash"
        crash_dir.mkdir(parents=True, exist_ok=True)
        # 路径对齐（T24）：veh_minidump 的 dmp 走 _crash_dir()，打包版 cwd 是
        # 安装目录（D:\...\Drifox）而 logs_dir 在 ~/.drifox/logs，两者不同源
        # → VEH 的 .dmp 与 faulthandler 的 .log 分居两处，现场取证断裂
        # （2026-09-16 实测复现）。此处写入环境变量强制统一，veh_minidump
        # 读取优先级 DRIFOX_CRASH_DIR > cwd 推导，故安装后永远与 crash_dir 一致。
        os.environ["DRIFOX_CRASH_DIR"] = str(crash_dir)
        stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
        dump_path = crash_dir / f"crash_{stamp}.log"
        _crash_file = open(dump_path, "w", encoding="utf-8")
        faulthandler.enable(file=_crash_file)
        _noise_file = open(crash_dir / f"anomaly_{stamp}.log", "w", encoding="utf-8")
        # 顺序敏感：分流 VEH 必须在 enable() 之后注册才能排到 faulthandler 之前
        _install_seh_classifier()
        _prune_anomaly_logs(crash_dir)
        atexit.register(_mark_clean_exit)
        _setup_wer_localdumps(crash_dir)
        # 取证增强：0xC0000409（qFatal/fastfail）不触发 faulthandler，
        # 0xC0000005 在 Windows 上走 SEH/WER 也不触发。两类崩溃此前只留下
        # 空文件无法定位 → qInstallMessageHandler 落 qFatal 文本（abort 前必经）。
        # 原 SetUnhandledExceptionFilter + MiniDumpWriteDump 链已删除：回调是
        # Python 函数，在异常上下文里跑字节码只产出 0 字节的 dmp，还会把
        # first-chance 异常升级成真崩溃（WER 签名 python314.dll / c000041d）。
        _install_qt_message_logger(crash_dir)
        # 全部子步骤成功后置位（失败分支在下面回滚，允许重试）
        _crash_handler_installed = True
        return dump_path
    except Exception:
        _crash_handler_installed = False
        return None


def _install_qt_message_logger(crash_dir: Path) -> None:
    """安装 Qt 消息钩子：warning 及以上按日期写入 qt_messages_YYYYMMDD.log。

    qFatal（触发 0xC0000409 fastfail，如跨线程 QPixmap / 跨线程事件）的
    致命文本在 abort 前必经本钩子，落盘后即可按文本定位崩溃源头。
    按日期分文件：单文件无轮转会无限膨胀，跨天自动切新文件，旧文件留档。
    """
    global _forensic_dir
    try:
        from PyQt5.QtCore import qInstallMessageHandler, QtMsgType

        def _qt_log_path() -> Path:
            return crash_dir / f"qt_messages_{time.strftime('%Y%m%d')}.log"

        _forensic_dir = crash_dir

        def _qt_handler(mode, context, message):
            try:
                label = {
                    QtMsgType.QtDebugMsg: "DEBUG",
                    QtMsgType.QtInfoMsg: "INFO",
                    QtMsgType.QtWarningMsg: "WARNING",
                    QtMsgType.QtCriticalMsg: "CRITICAL",
                    QtMsgType.QtFatalMsg: "FATAL",
                }.get(mode, "?")
                line = f"{time.strftime('%H:%M:%S')} [{label}] {message}\n"
                with open(_qt_log_path(), "a", encoding="utf-8", errors="replace") as f:
                    f.write(line)
                    if mode in (QtMsgType.QtFatalMsg, QtMsgType.QtCriticalMsg):
                        f.flush()
                        os.fsync(f.fileno())
            except Exception:  # noqa: BLE001
                pass

        qInstallMessageHandler(_qt_handler)
    except Exception:  # noqa: BLE001
        pass


def _is_noise_exception(code: int) -> bool:
    """异常码是否属于非致命噪声（上层处理器会消化，进程不会终止）。"""
    return code in _NOISE_EXCEPTION_CODES


def _install_seh_classifier() -> bool:
    """注册分流 VEH：非致命 SEH 异常的现场从 crash 文件改道到 anomaly 文件。

    手法（均已在 Python 3.14 实测，见 tests/debug/crash_filter_probe.py）：
    1. 在 faulthandler.enable() 之后以 first=1 注册，插到 VEH 链头，先于
       faulthandler 自己的 handler 被调用；
    2. 回调内按异常码再次调用 faulthandler.enable(file=...) 切换落点——
       二次 enable 幂等，只改输出目标，不会重复注册 handler；
    3. 返回 EXCEPTION_CONTINUE_SEARCH，不改写异常传播语义。

    回调刻意做到最轻：一次指针解引用 + 一次集合查找 + 一次 C 函数调用，
    不做 strftime / Path 拼接 / 文件 IO，降低在异常上下文里二次崩溃的风险。
    返回是否注册成功。

    幂等（T32）：已注册过则直接返回 True。重复注册会用新 thunk 覆盖
    ``_veh_handler_ref``，旧 thunk 随即被 GC 释放，而系统 VEH 链仍持有其
    地址 → 悬空指针（core 整跑必崩根因）。回调内部改读模块级
    ``_crash_file`` / ``_noise_file``，落点随下次 install 自动更新，
    无需重注册。
    """
    global _veh_handler_ref
    if _veh_handler_ref is not None:
        return True
    if sys.platform != "win32" or _crash_file is None or _noise_file is None:
        return False
    try:
        import ctypes
        import faulthandler
        from ctypes import wintypes

        EXCEPTION_CONTINUE_SEARCH = 0

        class _EXCEPTION_RECORD(ctypes.Structure):  # noqa: SLF001
            _fields_ = [
                ("ExceptionCode", wintypes.DWORD),
                ("ExceptionFlags", wintypes.DWORD),
                ("ExceptionRecord", ctypes.c_void_p),
                ("ExceptionAddress", ctypes.c_void_p),
                ("NumberParameters", wintypes.DWORD),
                ("ExceptionInformation", ctypes.c_size_t * 15),
            ]

        class _EXCEPTION_POINTERS(ctypes.Structure):  # noqa: SLF001
            _fields_ = [
                ("ExceptionRecord", ctypes.POINTER(_EXCEPTION_RECORD)),
                ("ContextRecord", ctypes.c_void_p),
            ]

        eps_ptr_type = ctypes.POINTER(_EXCEPTION_POINTERS)

        def _classify(exception_pointers: int) -> int:
            # 落点动态读模块全局（T32）：reset_for_test 后重新 install 会换新
            # 文件句柄，而无重注册的旧回调必须跟随新落点。句柄为空时跳过
            # （交 faulthandler 原落点），不传 None 给 enable 以免误关 faulthandler。
            try:
                record = ctypes.cast(exception_pointers, eps_ptr_type).contents.ExceptionRecord
                target = _noise_file if _is_noise_exception(record.contents.ExceptionCode) else _crash_file
                if target is not None:
                    faulthandler.enable(file=target)
            except BaseException:  # noqa: BLE001
                pass
            return EXCEPTION_CONTINUE_SEARCH

        cb = ctypes.WINFUNCTYPE(wintypes.LONG, ctypes.c_void_p)(_classify)
        _veh_handler_ref = cb  # 防 GC
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.AddVectoredExceptionHandler.restype = ctypes.c_void_p
        kernel32.AddVectoredExceptionHandler.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        return bool(kernel32.AddVectoredExceptionHandler(1, ctypes.cast(cb, ctypes.c_void_p)))
    except Exception:  # noqa: BLE001
        return False


def _prune_anomaly_logs(crash_dir: Path) -> None:
    """噪声日志按数量截断，只留最近 _ANOMALY_KEEP 份。"""
    try:
        files = sorted(crash_dir.glob("anomaly_*.log"), key=lambda p: p.stat().st_mtime)
        for stale in files[:-_ANOMALY_KEEP]:
            _silent_remove(stale)
    except Exception:  # noqa: BLE001
        pass


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
            winreg.SetValueEx(
                key, "DumpType", 0, winreg.REG_DWORD, 2
            )  # 2=完整 dump（含线程栈/寄存器上下文/全模块；1=minidump 实测仅模块表无法定位）
            winreg.SetValueEx(key, "DumpCount", 0, winreg.REG_DWORD, 50)
        return dumps_dir
    except Exception:
        return None


def _mark_clean_exit() -> None:
    """正常退出时打标记（记录性参考，已不再是崩溃判据）。

    真正区分「崩过」与「正常关闭」的是 crash 文件里有没异常段，
    见 check_pending_crashes 的理由说明。
    """
    try:
        if _crash_file is not None and not _crash_file.closed:
            _crash_file.write(_CLEAN_EXIT_MARK + "\n")
            _crash_file.close()
    except Exception:
        pass
    _discard_empty_anomaly_log()


def _discard_empty_anomaly_log() -> None:
    """噪声日志一无所获就删掉，避免每次启动堆一个空文件。"""
    global _noise_file
    try:
        if _noise_file is None:
            return
        path = Path(_noise_file.name)
        if not _noise_file.closed:
            _noise_file.close()
        if path.stat().st_size == 0:
            _silent_remove(path)
    except Exception:  # noqa: BLE001
        pass
    _noise_file = None


def check_pending_crashes(logs_dir: Path) -> list:
    """扫描 crash 目录，返回全部待报告的崩溃 dump（按崩溃时间从旧到新）。

    每份 dump 由 prompt_crash_report InfoBar 提示一次后重命名 .reported（改状态），
    因此这里只收集尚未报告的；未命中崩溃标记的文件一律保留不删（证据保全，
    空文件也可能是取证链未覆盖的崩溃形态，宁可留白不误删）。

    为何以「含异常段」而非「无 clean-exit 标记」为据：CPython 会把部分 SEH 异常
    （含原生 access violation）转成 Python 异常抛出，解释器随后走正常 shutdown，
    atexit 照样补得上 clean-exit 标记——只看标记会把真崩溃洗成「正常退出」。

    只扫 crash_*.log：非致命 SEH 异常已由 _install_seh_classifier 改道到
    anomaly_*.log，天然不参与崩溃判定。
    """
    try:
        crash_dir = Path(logs_dir) / "crash"
        if not crash_dir.is_dir():
            return []
        pending: list = []
        for f in crash_dir.glob("crash_*.log"):
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if EXCEPTION_MARK in content or VEH_MARK in content:
                pending.append(f)
            # 证据保全（T16 P0）：不再删除未命中标记的文件，杜绝误删真崩溃现场
        pending.sort(key=lambda p: p.stat().st_mtime)
        return pending
    except Exception:
        return []


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
    """以 InfoBar 横幅展示上次崩溃摘要，并提供打开报告目录的入口。

    InfoBar 持久显示（duration=-1，不自动消失，不打断当前操作）；
    创建成功后即重命名 .reported 标记已读，避免下次启动重复提示。
    """
    try:
        from PyQt5.QtCore import Qt, QUrl
        from PyQt5.QtGui import QDesktopServices
        from PyQt5.QtWidgets import QHBoxLayout, QWidget
        from qfluentwidgets import InfoBar, InfoBarIcon, InfoBarPosition, PushButton

        dump_path = Path(dump_path)
        try:
            crash_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(dump_path.stat().st_mtime))
        except Exception:
            crash_ts = "未知时间"

        # 关联同一次崩溃的系统级取证（±10 分钟窗口）
        wer_note = _nearby_wer_dump(dump_path) or _nearby_wer_report(dump_path)

        content = f"上次运行发生了原生崩溃（闪退），崩溃时间：{crash_ts}\n崩溃报告：{dump_path.name}"
        if wer_note:
            content += f"\n{wer_note}"

        bar = InfoBar(
            icon=InfoBarIcon.WARNING,
            title="检测到上次异常退出",
            content=content,
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            position=InfoBarPosition.BOTTOM,
            duration=-1,
            parent=parent,
        )
        open_btn = PushButton("打开报告目录")
        mute_btn = PushButton("后续不再显示")

        def _open_report_dir() -> None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(dump_path.parent)))
            bar.close()

        def _mute_crash_notifications() -> None:
            # 关闭「进入时崩溃通知」开关并立即落盘，后续启动不再弹横幅。
            # 与系统设置里的开关共用同一配置项，关闭后用户仍可在设置中重新开启。
            try:
                from app.utils.config import Settings

                cfg = Settings.get_instance()
                cfg.crash_notify_on_startup.value = False
                cfg.save()
                logger.info("[CrashHandler] 用户已在崩溃横幅上关闭进入时崩溃通知")
            except Exception:
                logger.warning("[CrashHandler] 关闭崩溃通知开关失败", exc_info=True)
            bar.close()

        open_btn.clicked.connect(_open_report_dir)
        mute_btn.clicked.connect(_mute_crash_notifications)
        # 按钮容器：弹性空间在左、按钮在右，匹配现代弹窗的「行动按钮右对齐」习惯。
        btn_row = QWidget(bar)
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(8)
        btn_layout.addStretch(1)
        btn_layout.addWidget(mute_btn)
        btn_layout.addWidget(open_btn)
        bar.addWidget(btn_row)
        bar.show()
        # 报告已告知 → 重命名标记已读：文件保留供排查（崩溃证据不可再生），
        # 后缀变化使 check_pending_crashes 不再命中，避免重复提示
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


def reset_crash_handler_for_test() -> None:
    """测试隔离：关闭本模块持有的文件句柄并允许重新安装（T32）。

    供单测在用例间复位状态。**刻意不调用 RemoveVectoredExceptionHandler**：
    移除 VEH 会释放回调 thunk 的引用，而操作系统 VEH 链里若仍残留其地址
    （移除失败的窗口期）就会造出新的悬空指针 —— 比留着旧回调更危险。
    旧回调继续存在也无害：它只调用 faulthandler.enable(file=...)，而 faulthandler
    的落点会随新 _crash_file 一起更新（安装时重设），异常分流语义保持正确。
    """
    global _crash_file, _noise_file, _crash_handler_installed, _forensic_dir
    for handle in (_crash_file, _noise_file):
        try:
            if handle is not None and not handle.closed:
                handle.close()
        except Exception:
            pass
    _crash_file = None
    _noise_file = None
    _forensic_dir = None
    _crash_handler_installed = False
