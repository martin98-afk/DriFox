# -*- coding: utf-8 -*-
"""
VEH 崩溃捕获器：native crash → minidump + C 栈 + Python 栈 + 模块审计

背景：Intel 核显驱动崩溃(igxelpicd64.dll)与后续暴露的 ctypes/libffi 崩溃
(python314.dll)都发生在 native 层，Python traceback 看不到调用链。
本捕获器在致命异常时自动落 logs/crash/*.dmp + *.log。

v2 增强（相对只打 C 栈的首版）：
  1. Python 栈：崩溃线程逐帧 文件:行号 in 函数 —— 定位"哪个 Python 槽发起
     的 ctypes 调用崩了"的唯一手段；其余线程同样展开（多 agent 并行场景
     下其他线程状态同样关键）。
  2. 模块审计：枚举进程内 libffi*/_ctypes*/python*/Qt5*/sip* 的实际加载
     路径 —— 分判 libffi-8.dll 多副本错载（打包版 DLL 冲突）。
  3. 寄存器：x64 CONTEXT 整型寄存器全量。
  4. 已知限制：MiniDumpWriteDump 的 ExceptionParam 在 in-process 调用下
     稳定返回 ERROR_NOACCESS(998)（实测 Win32 998，与 DumpType/传参方式
     无关），故 dmp 不携带异常上下文；崩溃线程的栈快照在 VEH 返回前抓取
     依然完整（VEH 回调帧上方即崩溃现场），异常点定位以 log 的 C 栈首帧
     + 寄存器为准。

用法（main.py 顶部，Qt 加载之前）：
    from app.utils.veh_minidump import install
    install()

回退：DRIFOX_NO_VEH=1 跳过安装。目录：DRIFOX_CRASH_DIR 覆盖默认 logs/crash。
约束：本模块禁止 import PyQt5（install 发生在 Qt 加载前）。
"""

import os
import sys
import time
import ctypes
from ctypes import (
    WINFUNCTYPE,
    Structure,
    POINTER,
    byref,
    c_void_p,
    c_uint64,
    c_ushort,
)
from ctypes.wintypes import (
    BOOL,
    DWORD,
    HANDLE,
    HMODULE,
    LONG,
    ULONG,
    UINT,
    LPCWSTR,
    LPVOID,
)

# ---------------------------------------------------------------------------
# Win32 常量与结构
# ---------------------------------------------------------------------------

EXCEPTION_ACCESS_VIOLATION = 0xC0000005
EXCEPTION_ILLEGAL_INSTRUCTION = 0xC000001D
EXCEPTION_STACK_OVERFLOW = 0xC00000FD
EXCEPTION_HEAP_CORRUPTION = 0xC0000374
STATUS_FAIL_FAST = 0xC0000409

_FATAL_CODES = {
    EXCEPTION_ACCESS_VIOLATION,
    EXCEPTION_ILLEGAL_INSTRUCTION,
    EXCEPTION_STACK_OVERFLOW,
    EXCEPTION_HEAP_CORRUPTION,
    STATUS_FAIL_FAST,
}

EXCEPTION_MAXIMUM_PARAMETERS = 15
EXCEPTION_CONTINUE_SEARCH = 0

MAX_C_STACK_FRAMES = 64
MAX_PY_STACK_DEPTH = 80
MAX_PY_THREADS = 24

# MiniDump type: Normal | WithIndirectlyReferencedMemory | WithUnloadedModules
MiniDumpType = 0x40 | 0x20


class EXCEPTION_RECORD(Structure):
    _fields_ = [
        ("ExceptionCode", DWORD),
        ("ExceptionFlags", DWORD),
        ("ExceptionRecord", c_void_p),
        ("ExceptionAddress", c_void_p),
        ("NumberParameters", DWORD),
        ("ExceptionInformation", ULONG * EXCEPTION_MAXIMUM_PARAMETERS),
    ]


class _CONTEXT_HEAD(Structure):
    """x64 CONTEXT 前缀（到 RIP 为止，仅用于读寄存器，后缀浮点区不定义）。"""

    _pack_ = 16
    _fields_ = [
        ("P1Home", c_uint64),
        ("P2Home", c_uint64),
        ("P3Home", c_uint64),
        ("P4Home", c_uint64),
        ("P5Home", c_uint64),
        ("P6Home", c_uint64),
        ("ContextFlags", DWORD),
        ("MxCsr", DWORD),
        ("SegCs", c_ushort),
        ("SegDs", c_ushort),
        ("SegEs", c_ushort),
        ("SegFs", c_ushort),
        ("SegGs", c_ushort),
        ("SegSs", c_ushort),
        ("EFlags", DWORD),
        ("Dr0", c_uint64),
        ("Dr1", c_uint64),
        ("Dr2", c_uint64),
        ("Dr3", c_uint64),
        ("Dr6", c_uint64),
        ("Dr7", c_uint64),
        ("Rax", c_uint64),
        ("Rcx", c_uint64),
        ("Rdx", c_uint64),
        ("Rbx", c_uint64),
        ("Rsp", c_uint64),
        ("Rbp", c_uint64),
        ("Rsi", c_uint64),
        ("Rdi", c_uint64),
        ("R8", c_uint64),
        ("R9", c_uint64),
        ("R10", c_uint64),
        ("R11", c_uint64),
        ("R12", c_uint64),
        ("R13", c_uint64),
        ("R14", c_uint64),
        ("R15", c_uint64),
        ("Rip", c_uint64),
    ]


class EXCEPTION_POINTERS(Structure):
    _fields_ = [
        ("ExceptionRecord", POINTER(EXCEPTION_RECORD)),
        ("ContextRecord", POINTER(_CONTEXT_HEAD)),
    ]


# ---------------------------------------------------------------------------
# 模块级状态（防重入 + 保住回调引用不被 GC）
# ---------------------------------------------------------------------------

_handler_ref = None   # WINFUNCTYPE 回调必须全局持有，否则 GC 后 VEH 悬空
_busy = False

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dbg = None  # dbghelp 懒加载


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _crash_dir():
    d = os.environ.get("DRIFOX_CRASH_DIR") or os.path.join("logs", "crash")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = "."
    return d


def _exc_desc(code):
    return {
        EXCEPTION_ACCESS_VIOLATION: "ACCESS_VIOLATION",
        EXCEPTION_ILLEGAL_INSTRUCTION: "ILLEGAL_INSTRUCTION",
        EXCEPTION_STACK_OVERFLOW: "STACK_OVERFLOW",
        EXCEPTION_HEAP_CORRUPTION: "HEAP_CORRUPTION",
        STATUS_FAIL_FAST: "FAIL_FAST",
    }.get(code, "0x%08X" % (code & 0xFFFFFFFF))


def _module_map():
    """返回 [(模块文件名, 全路径, base, size), ...]，失败返回 []。

    注意：K32EnumProcessModules 首次以 cb=0 探测数量时返回 0 是预期行为
    （ERROR_INSUFFICIENT_BUFFER），不可当作失败。
    """
    try:
        k32 = _k32
        k32.GetCurrentProcess.restype = HANDLE
        hproc = HANDLE(k32.GetCurrentProcess())
        # 伪句柄 -1 等值超出 32 位，所有 API 必须显式 argtypes，否则 OverflowError
        k32.K32EnumProcessModules.argtypes = [HANDLE, POINTER(HMODULE), DWORD, POINTER(DWORD)]
        k32.K32EnumProcessModules.restype = BOOL
        k32.K32GetModuleBaseNameW.argtypes = [HANDLE, HANDLE, LPCWSTR, DWORD]
        k32.K32GetModuleBaseNameW.restype = DWORD
        k32.K32GetModuleFileNameExW.argtypes = [HANDLE, HANDLE, LPCWSTR, DWORD]
        k32.K32GetModuleFileNameExW.restype = DWORD

        needed = DWORD(0)
        # 第一次调用：只探测所需字节数，忽略返回值
        k32.K32EnumProcessModules(hproc, None, 0, byref(needed))
        count = needed.value // ctypes.sizeof(HMODULE)
        if count <= 0 or count > 8192:
            return []
        arr = (HMODULE * count)()
        if not k32.K32EnumProcessModules(
            hproc, arr, ctypes.sizeof(arr), byref(needed)
        ):
            return []

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetModuleInformation.restype = BOOL
        psapi.GetModuleInformation.argtypes = [HANDLE, HANDLE, LPVOID, DWORD]

        class MODULEINFO(Structure):
            _fields_ = [
                ("lpBaseOfDll", LPVOID),
                ("SizeOfImage", DWORD),
                ("EntryPoint", LPVOID),
            ]

        out = []
        real = needed.value // ctypes.sizeof(HMODULE)
        namebuf = ctypes.create_unicode_buffer(1024)
        for i in range(min(real, count)):
            mi = MODULEINFO()
            if psapi.GetModuleInformation(hproc, HANDLE(arr[i]), byref(mi), ctypes.sizeof(mi)):
                k32.K32GetModuleBaseNameW(hproc, HANDLE(arr[i]), namebuf, 1024)
                short = namebuf.value
                k32.K32GetModuleFileNameExW(hproc, HANDLE(arr[i]), namebuf, 1024)
                full = namebuf.value
                out.append((short, full, mi.lpBaseOfDll or 0, mi.SizeOfImage or 0))
        return out
    except Exception:
        return []


def _addr_to_module(addr, mods):
    for short, full, base, size in mods:
        if base <= addr < base + size:
            return short, full, addr - base
    return None, None, 0


def _capture_c_stack(mods):
    """RtlCaptureStackBackTrace + 模块!RVA 解析。"""
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.RtlCaptureStackBackTrace.restype = c_ushort
    ntdll.RtlCaptureStackBackTrace.argtypes = [
        DWORD, DWORD, POINTER(c_void_p), POINTER(DWORD)
    ]
    arr = (c_void_p * MAX_C_STACK_FRAMES)()
    skipped = DWORD(0)
    # 跳过本函数与 VEH 回调自身 2 帧
    n = ntdll.RtlCaptureStackBackTrace(2, MAX_C_STACK_FRAMES, arr, byref(skipped))
    lines = []
    for i in range(n or 0):
        a = arr[i] or 0
        short, full, rva = _addr_to_module(a, mods)
        if short:
            lines.append("#%02d 0x%016X %s+0x%X" % (i, a, short, rva))
        else:
            lines.append("#%02d 0x%016X <unknown>" % (i, a))
    return lines


def _dump_python_stack_chain(top_frame):
    """把一条 frame 链转成 文件:行号 in 函数 文本块。"""
    lines = []
    f = top_frame
    depth = 0
    while f is not None and depth < MAX_PY_STACK_DEPTH:
        try:
            code = f.f_code
            lines.append(
                "  py#%02d %s:%s in %s"
                % (depth, code.co_filename, f.f_lineno, code.co_name)
            )
        except Exception:
            lines.append("  py#%02d <unreachable frame>" % depth)
        f = f.f_back
        depth += 1
    return lines


def _dump_python_stacks(crash_tid):
    """全线程 Python 栈；崩溃线程(按 native_id 对照)标 ★ 并排在最前。"""
    import threading

    out = []
    try:
        idents = sys._current_frames()
    except Exception as e:
        return ["  <sys._current_frames failed: %r>" % (e,)]

    # ident -> (name, native_id)
    meta = {}
    try:
        for t in threading.enumerate():
            meta[t.ident] = (t.name, getattr(t, "native_id", None))
    except Exception:
        pass

    crash_ident = None
    for ident in idents:
        if meta.get(ident, (None, None))[1] == crash_tid:
            crash_ident = ident
            break

    ordered = list(idents.items())
    if crash_ident is not None:
        ordered.sort(key=lambda kv: 0 if kv[0] == crash_ident else 1)

    for ident, frame in ordered[:MAX_PY_THREADS]:
        name, native = meta.get(ident, ("<unknown>", None))
        star = "★ crash" if ident == crash_ident else ""
        out.append("thread ident=%s native=%s name=%s %s" % (ident, native, name, star))
        out.extend(_dump_python_stack_chain(frame))
    if len(idents) > MAX_PY_THREADS:
        out.append("  <... %d more threads truncated>" % (len(idents) - MAX_PY_THREADS))
    return out


def _module_audit(mods):
    """关键二进制审计：libffi/_ctypes/python/Qt5/sip/PyQt 实际加载路径。"""
    keys = ("libffi", "_ctypes", "python3", "python.exe", "qt5core", "qt5widgets",
            "qt5gui", "sip.", "pyqt5", "qwindows")
    lines = []
    for short, full, base, size in mods:
        low = short.lower()
        if any(k in low for k in keys):
            lines.append(
                "  %-28s base=0x%016X size=0x%X\n      %s" % (short, base, size, full)
            )
    return lines


def _write_minidump(dmp_path):
    """MiniDumpWriteDump（ExceptionParam=None，见文件头"已知限制"）。

    返回 (ok, info)。
    """
    global _dbg
    try:
        if _dbg is None:
            _dbg = ctypes.WinDLL("dbghelp", use_last_error=True)
        k32 = _k32
        k32.GetCurrentProcess.restype = HANDLE
        hproc = k32.GetCurrentProcess()
        k32.GetCurrentProcessId.restype = DWORD

        GENERIC_WRITE = 0x40000000
        GENERIC_READ = 0x80000000
        CREATE_ALWAYS = 2
        FILE_ATTRIBUTE_NORMAL = 0x80
        FILE_SHARE_READ = 1

        k32.CreateFileW.restype = HANDLE
        k32.CreateFileW.argtypes = [LPCWSTR, DWORD, DWORD, LPVOID, DWORD, DWORD, HANDLE]
        hf = k32.CreateFileW(
            dmp_path, GENERIC_WRITE | GENERIC_READ, FILE_SHARE_READ,
            None, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, None,
        )
        if hf in (None, HANDLE(-1).value):
            return False, "CreateFileW failed err=%d" % ctypes.get_last_error()

        _dbg.MiniDumpWriteDump.restype = BOOL
        _dbg.MiniDumpWriteDump.argtypes = [
            HANDLE, DWORD, HANDLE, UINT, LPVOID, LPVOID, LPVOID,
        ]
        ok = _dbg.MiniDumpWriteDump(
            hproc, k32.GetCurrentProcessId(), hf, MiniDumpType, None, None, None,
        )
        err = ctypes.get_last_error()
        k32.CloseHandle.argtypes = [HANDLE]
        k32.CloseHandle(hf)
        if not ok:
            return False, "MiniDumpWriteDump failed err=%d" % err
        size = os.path.getsize(dmp_path) if os.path.exists(dmp_path) else 0
        if size == 0:
            return False, "minidump written but 0 bytes"
        return True, "size=%d (no exception context, see log C stack)" % size
    except Exception as e:
        return False, "exception: %r" % (e,)


# ---------------------------------------------------------------------------
# VEH 回调
# ---------------------------------------------------------------------------


def _veh_handler(ptrs):
    global _busy
    try:
        rec = ptrs.contents.ExceptionRecord.contents
        code = rec.ExceptionCode & 0xFFFFFFFF
    except Exception:
        return EXCEPTION_CONTINUE_SEARCH

    # 高频/非致命异常立即放行（断点、单步、CLR、应用自定义异常等）
    if code not in _FATAL_CODES:
        return EXCEPTION_CONTINUE_SEARCH
    if _busy:
        return EXCEPTION_CONTINUE_SEARCH
    _busy = True
    try:
        _handle_crash(ptrs, code)
    except Exception:
        pass
    return EXCEPTION_CONTINUE_SEARCH


def _handle_crash(ptrs, code):
    k32 = _k32
    k32.GetCurrentThreadId.restype = DWORD
    k32.GetCurrentProcessId.restype = DWORD
    tid = k32.GetCurrentThreadId()
    pid = k32.GetCurrentProcessId()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = os.path.join(_crash_dir(), "crash_%s_pid_%d" % (stamp, pid))
    log_path = base + ".log"
    dmp_path = base + ".dmp"

    mods = _module_map()

    lines = []
    lines.append("[DRIFOX VEH v2] native crash captured")
    lines.append("time=%s pid=%d tid=%d" % (_now_str(), pid, tid))
    lines.append("python=%s" % sys.version.replace("\n", " "))
    lines.append("executable=%s" % sys.executable)

    rec = ptrs.contents.ExceptionRecord.contents
    lines.append(
        "exception=%s at 0x%016X flags=%d"
        % (_exc_desc(code), rec.ExceptionAddress or 0, rec.ExceptionFlags)
    )
    if code == EXCEPTION_ACCESS_VIOLATION and rec.NumberParameters >= 2:
        mode = "READ" if rec.ExceptionInformation[0] == 0 else "WRITE"
        lines.append("access=%s target=0x%X" % (mode, rec.ExceptionInformation[1]))

    # 寄存器（崩溃现场上下文）
    try:
        ctx = ptrs.contents.ContextRecord.contents
        lines.append("--- registers ---")
        for r in ("Rip", "Rax", "Rbx", "Rcx", "Rdx", "Rsi", "Rdi",
                  "R8", "R9", "R10", "R11", "R12", "R13", "R14", "R15", "Rsp", "Rbp"):
            lines.append("%s=0x%016X" % (r, getattr(ctx, r)))
    except Exception as e:
        lines.append("--- registers unavailable: %r ---" % (e,))

    # Python 栈（崩溃线程标 ★ 全量，其余线程展开）
    if code == EXCEPTION_STACK_OVERFLOW:
        lines.append("--- python stacks skipped (stack overflow) ---")
    else:
        lines.append("--- python stacks (crash thread marked ★) ---")
        lines.extend(_dump_python_stacks(tid))

    # C 栈
    try:
        lines.append("--- C stack (max %d frames) ---" % MAX_C_STACK_FRAMES)
        lines.extend(_capture_c_stack(mods))
    except Exception as e:
        lines.append("--- C stack unavailable: %r ---" % (e,))

    # 模块审计
    lines.append("--- module audit (ffi/ctypes/python/qt/sip) ---")
    lines.extend(_module_audit(mods) or ["  <enum failed>"])

    # minidump（无异常上下文，崩溃点以 log 为准）
    ok, info = _write_minidump(dmp_path)
    lines.append("--- minidump ---")
    lines.append("%s: %s" % ("saved" if ok else "FAILED", info))

    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        # 最后兜底：stderr（控制台/重定向场景）
        try:
            sys.stderr.write("\n".join(lines) + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 安装入口
# ---------------------------------------------------------------------------


def install():
    """安装 VEH。必须在 QApplication 之前调用；失败静默（绝不阻塞启动）。"""
    global _handler_ref
    if os.environ.get("DRIFOX_NO_VEH", "").strip() in ("1", "true", "on"):
        return False
    if _handler_ref is not None:
        return True

    proto = WINFUNCTYPE(LONG, POINTER(EXCEPTION_POINTERS))
    _handler_ref = proto(_veh_handler)
    if not _k32.AddVectoredExceptionHandler(ULONG(1), _handler_ref):
        _handler_ref = None
        return False
    return True


def uninstall():
    """测试/卸载用：移除 VEH（同一 handler 指针）。"""
    global _handler_ref
    if _handler_ref is not None:
        _k32.RemoveVectoredExceptionHandler(_handler_ref)
        _handler_ref = None
