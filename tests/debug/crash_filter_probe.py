# -*- coding: utf-8 -*-
"""崩溃捕获链探针：确认 faulthandler 在 Windows 上能否按异常码分流

目的（排查 crash_handler 误报时建立反馈循环）：
1. baseline —— faulthandler 是否真的无过滤地记录非致命码（0x8001010d）
2. renable  —— 二次 enable(file=B) 是否重复注册 VEH（决定能否用它切文件）
3. dup2     —— 只换 fd 指向能否改变 faulthandler 的落点（备选分流手段）
4. filtered —— 自建 VEH 抢在 faulthandler 前、按码切文件，能否正确分流

用法：python tests/debug/crash_filter_probe.py
每种模式在独立子进程里跑（异常一抛进程即终止，无法原地恢复）。
"""

import ctypes
import faulthandler
import os
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

CODE_NOISE = 0x8001010D  # RPC_E_CANTCALLOUT_ININPUTSYNCCALL
CODE_FATAL = 0xC0000005  # EXCEPTION_ACCESS_VIOLATION

EXCEPTION_CONTINUE_SEARCH = 0

kernel32 = ctypes.WinDLL("kernel32")
kernel32.AddVectoredExceptionHandler.restype = ctypes.c_void_p
kernel32.AddVectoredExceptionHandler.argtypes = [wintypes.DWORD, ctypes.c_void_p]


class EXCEPTION_RECORD(ctypes.Structure):
    _fields_ = [
        ("ExceptionCode", wintypes.DWORD),
        ("ExceptionFlags", wintypes.DWORD),
        ("ExceptionRecord", ctypes.c_void_p),
        ("ExceptionAddress", ctypes.c_void_p),
        ("NumberParameters", wintypes.DWORD),
        ("ExceptionInformation", ctypes.c_size_t * 15),
    ]


class EXCEPTION_POINTERS(ctypes.Structure):
    _fields_ = [
        ("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
        ("ContextRecord", ctypes.c_void_p),
    ]


VEH_CB = ctypes.WINFUNCTYPE(wintypes.LONG, ctypes.c_void_p)


def _read_code(ptr: int) -> int:
    """从 EXCEPTION_POINTERS 裸指针取异常码。"""
    eps = ctypes.cast(ptr, ctypes.POINTER(EXCEPTION_POINTERS)).contents
    return eps.ExceptionRecord.contents.ExceptionCode


def _open(name: str, d: str):
    # buffering=1 行缓冲：尽量贴近 faulthandler 的低层 write，避免整块缓冲丢数据
    return open(os.path.join(d, name), "w", encoding="utf-8", buffering=1)


def _raise(code: int) -> None:
    kernel32.RaiseException(wintypes.DWORD(code), 0, 0, None)
    print(f"RaiseException({code:#x}) returned, proc alive", flush=True)


# ---------------- 子进程模式 ----------------


def child_baseline(d: str) -> None:
    f1 = _open("f1.log", d)
    faulthandler.enable(file=f1)
    _raise(CODE_NOISE)


def child_renable(d: str) -> None:
    f1 = _open("f1.log", d)
    f2 = _open("f2.log", d)
    faulthandler.enable(file=f1)
    faulthandler.enable(file=f2)  # 关键：是否又插了一个 VEH
    _raise(CODE_FATAL)


def child_dup2(d: str) -> None:
    f1 = _open("f1.log", d)
    faulthandler.enable(file=f1)
    f2 = _open("f2.log", d)
    os.dup2(f2.fileno(), f1.fileno())  # f1 的 fd 号不变，指向换到 f2
    _raise(CODE_FATAL)


def child_filtered(d: str, which: str) -> None:
    """模拟目标实现：默认落 crash 文件，命中噪声码切到 anomaly 文件。"""
    crash_f = _open("crash.log", d)
    noise_f = _open("anomaly.log", d)
    faulthandler.enable(file=crash_f)

    def _veh(ptr: int) -> int:
        try:
            code = _read_code(ptr)
            faulthandler.enable(file=noise_f if code == CODE_NOISE else crash_f)
        except BaseException:
            pass
        return EXCEPTION_CONTINUE_SEARCH

    cb = VEH_CB(_veh)
    # 后注册 + first=1 → 排在 faulthandler 的 VEH 之前
    kernel32.AddVectoredExceptionHandler(1, ctypes.cast(cb, ctypes.c_void_p))
    cb._keep = cb  # noqa: SLF001
    _raise(CODE_NOISE if which == "noise" else CODE_FATAL)


def child_handler_noise(d: str) -> None:
    """端到端：真实 install_crash_handler + 非致命码 → 应落 anomaly，不落 crash。"""
    from app.core.infra.crash_handler import install_crash_handler

    install_crash_handler(d)
    _raise(CODE_NOISE)


def child_handler_fatal(d: str) -> None:
    from app.core.infra.crash_handler import install_crash_handler

    install_crash_handler(d)
    _raise(CODE_FATAL)


def child_handler_clean(d: str) -> None:
    """端到端：噪声异常后正常退出 → crash 带 clean-exit 标记，空 anomaly 被回收。"""
    import atexit

    from app.core.infra.crash_handler import _mark_clean_exit, install_crash_handler

    install_crash_handler(d)
    atexit.register(_mark_clean_exit)
    _raise(CODE_NOISE)
    sys.exit(0)  # 走到这里 = 噪声确实没杀死进程


CHILDREN = {
    "baseline": child_baseline,
    "renable": child_renable,
    "dup2": child_dup2,
    "filtered-noise": lambda d: child_filtered(d, "noise"),
    "filtered-fatal": lambda d: child_filtered(d, "fatal"),
    "handler-noise": child_handler_noise,
    "handler-fatal": child_handler_fatal,
    "handler-clean": child_handler_clean,
}


def run_child(mode: str) -> None:
    CHILDREN[mode](os.environ["PROBE_DIR"])


# 期望矩阵：每种模式下各文件前缀应有的异常段数；ABSENT=文件应已被回收
ABSENT = -1
EXPECT = {
    "handler-noise": ({"crash_": 0, "anomaly_": 1}, "非致命码必须只落 anomaly"),
    "handler-fatal": ({"crash_": 1, "anomaly_": ABSENT}, "致命码必须落 crash"),
    "handler-clean": ({"crash_": 0, "anomaly_": 1}, "噪声后正常退出：crash 应无异常段"),
}
NEED_CLEAN = {"handler-clean": "crash_"}
# 用户可见结果：下次启动应不应弹「上次异常退出」报告
PENDING_EXPECT = {"handler-noise": 0, "handler-fatal": 1, "handler-clean": 0}


def _pick(files: dict, prefix: str):
    for k, v in files.items():
        if k.startswith(prefix):
            return k, v
    return None, (None, None)


def _verify(mode: str, files: dict) -> str:
    spec = EXPECT.get(mode)
    if not spec:
        return ""
    prefixes, note = spec
    bad = []
    for prefix, want in prefixes.items():
        name, (paras, _clean) = _pick(files, prefix)
        if want is ABSENT:
            if name is not None and paras:
                bad.append(f"{prefix}* 本应被回收, 实有异常段={paras}")
            continue
        if name is None:
            bad.append(f"{prefix}* 文件缺失")
        elif paras != want:
            bad.append(f"{prefix}* 异常段={paras} 期望={want}")
    clean_prefix = NEED_CLEAN.get(mode)
    if clean_prefix:
        if not _pick(files, clean_prefix)[1][1]:
            bad.append(f"{clean_prefix}* 缺 clean-exit 标记")
    return ("  ✗ " + note + ": " + "; ".join(bad)) if bad else "  ✓ 分流符合预期"


def drive() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else None
    print(f"python={sys.version.split()[0]}")
    failures = []
    for mode in CHILDREN:
        if only and mode != only:
            continue
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "PROBE_DIR": d, "PYTHONIOENCODING": "utf-8"}
            r = subprocess.run(
                [sys.executable, __file__, "--child", mode],
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
            )
            files = {}
            # install_crash_handler 把日志放在 logs_dir/crash/ 下，需多扫一层
            scan = [d] + [os.path.join(d, s) for s in os.listdir(d) if os.path.isdir(os.path.join(d, s))]
            for sd in scan:
                for name in sorted(os.listdir(sd)):
                    if not name.endswith(".log"):
                        continue
                    text = open(os.path.join(sd, name), encoding="utf-8", errors="replace").read()
                    files[name] = (text.count("Windows fatal exception"), "clean exit" in text)
            print(f"\n[{mode}] rc={r.returncode}")
            for name, (paras, clean) in files.items():
                print(f"   {name:<26} 异常段={paras} clean_exit={'是' if clean else '否'}")
            if not files:
                print("   (无 .log 输出)")
            verdict = _verify(mode, files)
            print(verdict)
            if verdict.startswith("  ✗"):
                failures.append(mode)
            if mode in PENDING_EXPECT:
                from app.core.infra.crash_handler import check_pending_crashes

                got_pending = len(check_pending_crashes(Path(d)))
                want_pending = PENDING_EXPECT[mode]
                mark = "✓" if got_pending == want_pending else "✗"
                print(f"   {mark} 下次启动报告数={got_pending} 期望={want_pending}")
                if got_pending != want_pending:
                    failures.append(mode + ":pending")
                # check_pending_crashes 会就地删噪声日志，扫前已取内容不影响断言
            if r.stderr.strip():
                print("   stderr tail:")
                for line in r.stderr.strip().splitlines()[-3:]:
                    print("     " + line[:150])
    print(f"\n==> 失败模式: {failures if failures else '无'}")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        run_child(sys.argv[2])
        sys.exit(0)
    sys.exit(drive())
