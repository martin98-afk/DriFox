# -*- coding: utf-8 -*-
"""内存绝对上限门（H2）：启动后稳态的 phys_footprint 上限（主进程 + 全树）。

为什么需要它
------------
既有内存测试只做两件事：
- `test_memory_baseline.py` 四个场景**只 print 不断言上限**（"total > 0" 只能证明采集没坏）
- `test_perf_regression.py` 需 `PERF_TESTS=1` 且阈值松（10 个 1MB fake tab）

即：稳态 RSS 翻倍不会有任何测试报警。

口径
----
- 读 ``proc_pid_rusage`` 的 ``ri_phys_footprint``（offset 72），不用 RSS：
  macOS RSS 含共享页会虚高，与系统 ``footprint`` 工具口径不一致。
  本文件内的读法已与 ``footprint -p <pid>`` 交叉验证过（同进程同值）。
- **主进程** = 被测 main.py 进程自身。
- **全树** = 主进程 + 其全部子进程（QtWebEngineProcess 等），递归取尽。
- 稳态 = 首窗打点后再等 ``STEADY_WAIT_S`` 秒，取末段 ``TAIL_WINDOW_S`` 内中位数。

环境纪律
--------
与启动门一致：脏环境 skip（或非严格模式下仅警告后继续），不给假绿。
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ENTRY = _ROOT / "main.py"
_PY = sys.executable
_MARK_RE = re.compile(r"^\[StartupMark\] (\S+) 耗时 (\d+)ms")

# 阈值：当前实测（空载稳态）主进程 ~351MB、全树 ~406MB。
# 留足余量 —— 宁松不紧，先立起来再收紧。可用环境变量覆盖。
MAIN_BUDGET_MB = float(os.environ.get("DRIFOX_MEM_MAIN_BUDGET_MB", "800"))
TREE_BUDGET_MB = float(os.environ.get("DRIFOX_MEM_TREE_BUDGET_MB", "1200"))

STEADY_WAIT_S = float(os.environ.get("DRIFOX_MEM_STEADY_WAIT_S", "6"))
TAIL_WINDOW_S = 1.5
SAMPLE_INTERVAL_S = 0.25
_LOAD_RATIO_MAX = 0.8

# proc_pid_rusage 缓冲：系统按 flavor 写完整结构体，不足会越界写（既有实现留 1KB）
_FP_BUF = 1024
_FP_OFFSET = 16 + 7 * 8  # ri_uuid[16] + 前 7 个 uint64 = ri_phys_footprint
_FP_FLAVOR = 4

pytestmark = pytest.mark.perf

_LIBC = None


def _load_libc():
    global _LIBC
    if _LIBC is None:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        lib.proc_pid_rusage.restype = ctypes.c_int
        _LIBC = lib
    return _LIBC


def footprint_mb(pid: int) -> float:
    """指定进程的 phys_footprint（MB）；读取失败返回 0.0。"""
    if sys.platform != "darwin":
        return 0.0
    try:
        lib = _load_libc()
    except OSError:
        return 0.0
    buf = ctypes.create_string_buffer(_FP_BUF)
    if lib.proc_pid_rusage(pid, _FP_FLAVOR, buf) != 0:
        return 0.0
    return ctypes.c_uint64.from_buffer(buf, _FP_OFFSET).value / 1048576.0


def _child_pids(pid: int) -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except (subprocess.SubprocessError, ValueError):
        return []


def _tree_pids(pid: int) -> list[int]:
    """递归取进程树（含自身）；用已见集合防环。"""
    seen: list[int] = []
    stack = [pid]
    guard: set[int] = set()
    while stack:
        cur = stack.pop()
        if cur in guard:
            continue
        guard.add(cur)
        seen.append(cur)
        stack.extend(_child_pids(cur))
    return seen


def tree_footprint_mb(pid: int) -> float:
    """主进程 + 全部后代进程的 phys_footprint 之和。"""
    return sum(footprint_mb(p) for p in _tree_pids(pid))


def _ancestor_pids(pid: int) -> set[int]:
    out: set[int] = set()
    cur = pid
    for _ in range(32):
        try:
            proc = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(cur)],
                capture_output=True, text=True, timeout=5,
            )
            ppid = int(proc.stdout.strip() or 0)
        except (subprocess.SubprocessError, ValueError):
            break
        if ppid <= 1 or ppid in out:
            break
        out.add(ppid)
        cur = ppid
    return out


def _skip_if_env_dirty() -> None:
    ncpu = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = 0.0
    if load1 > ncpu * _LOAD_RATIO_MAX:
        pytest.skip(
            f"measurement env not clean: load1={load1:.2f} > {ncpu * _LOAD_RATIO_MAX:.1f}"
        )
    me = os.getpid()
    ancestors = _ancestor_pids(me)
    try:
        proc = subprocess.run(
            ["pgrep", "-f", str(_ENTRY)], capture_output=True, text=True, timeout=10
        )
        candidates = [int(x) for x in proc.stdout.split() if x.strip().isdigit()]
    except (subprocess.SubprocessError, ValueError):
        candidates = []
    others = [p for p in candidates if p != me and p not in ancestors]
    if others:
        msg = f"{len(others)} 个 main.py 实例残留: {others[:5]}"
        if os.environ.get("DRIFOX_PERF_STRICT_ENV") == "1":
            pytest.skip(f"measurement env not clean: {msg}")
        print(f"\n[warn] {msg}（非严格模式，继续测量）", flush=True)


def _stop_child(proc: "subprocess.Popen[str]") -> None:
    """收尾被测子进程；失败静默（不让测试因清理失败而报错）。"""
    if proc.poll() is not None:
        return
    import signal as _signal

    for sig in (_signal.SIGTERM, _signal.SIGKILL):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            return
        try:
            proc.wait(timeout=15)
            return
        except subprocess.TimeoutExpired:
            continue


class _Sampler(threading.Thread):
    """固定间隔采样主进程与全树 phys_footprint。"""

    def __init__(self, pid: int, interval: float) -> None:
        super().__init__(daemon=True, name="mem-budget-sampler")
        self.pid = pid
        self.interval = interval
        self.samples: list[tuple[float, float, float]] = []
        self._stop = threading.Event()

    def run(self) -> None:
        t0 = time.perf_counter()
        while not self._stop.is_set():
            main = footprint_mb(self.pid)
            tree = tree_footprint_mb(self.pid)
            if main > 0:
                self.samples.append((time.perf_counter() - t0, main, tree))
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def _measure_idle() -> tuple[float, float, list[tuple[float, float, float]]]:
    """拉起真进程 → 等首窗 → 采样到稳态 → 返回 (main_steady, tree_steady, samples)。"""
    data_dir = Path(tempfile.mkdtemp(prefix="drifox_mem_budget_"))
    src_cfg = _ROOT / ".drifox" / "app.config"
    if src_cfg.exists():
        shutil.copy2(src_cfg, data_dir / "app.config")

    env = os.environ.copy()
    env.update(
        {
            "DRIFOX_STARTUP_PROBE": "1",
            "DRIFOX_STARTUP_PROBE_EXIT_MS": str(int((STEADY_WAIT_S + 30) * 1000)),
            "DRIFOX_DATA_DIR": str(data_dir),
            "PYTHONPATH": str(_ROOT)
            + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
        }
    )
    env.pop("QT_QPA_PLATFORM", None)

    proc = subprocess.Popen(
        [_PY, str(_ENTRY)],
        cwd=str(data_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )
    sampler = _Sampler(proc.pid, SAMPLE_INTERVAL_S)
    try:
        assert proc.stderr is not None
        # 等首窗打点（读到即停，不依赖 EOF）
        while True:
            line = proc.stderr.readline()
            if not line:
                break
            m = _MARK_RE.match(line)
            if m and m.group(1) == "first_chat_window":
                break
        sampler.start()
        time.sleep(STEADY_WAIT_S)
    finally:
        sampler.stop()
        sampler.join(timeout=5)
        _stop_child(proc)
        shutil.rmtree(data_dir, ignore_errors=True)

    samples = sampler.samples
    if not samples:
        return 0.0, 0.0, []
    last_t = samples[-1][0]
    tail = [s for s in samples if s[0] >= last_t - TAIL_WINDOW_S] or samples[-1:]
    main_steady = statistics.median(s[1] for s in tail)
    tree_steady = statistics.median(s[2] for s in tail)
    return main_steady, tree_steady, samples


def test_idle_main_process_within_budget() -> None:
    """空载启动稳态：主进程 phys_footprint 必须低于上限（默认 800MB）。"""
    if sys.platform != "darwin":
        pytest.skip("phys_footprint 仅 macOS 可用")
    _skip_if_env_dirty()

    main_steady, tree_steady, samples = _measure_idle()
    assert samples, "未采集到任何有效内存样本（phys_footprint 读取失败或进程未起来）"
    peak_main = max(s[1] for s in samples)
    print(
        f"\n[内存预算·主进程] 稳态={main_steady:.0f}MB 峰值={peak_main:.0f}MB "
        f"全树稳态={tree_steady:.0f}MB 预算={MAIN_BUDGET_MB:.0f}MB "
        f"采样={len(samples)}点/{samples[-1][0]:.1f}s",
        flush=True,
    )
    assert main_steady < MAIN_BUDGET_MB, (
        f"空载稳态主进程 {main_steady:.0f}MB 超预算 {MAIN_BUDGET_MB:.0f}MB"
        f"（峰值 {peak_main:.0f}MB）。先复测基线再调 DRIFOX_MEM_MAIN_BUDGET_MB，"
        f"不要为了变绿而放宽。"
    )


def test_idle_tree_within_budget() -> None:
    """空载启动稳态：主进程 + 全部子进程（renderer 等）合计必须低于上限（默认 1200MB）。"""
    if sys.platform != "darwin":
        pytest.skip("phys_footprint 仅 macOS 可用")
    _skip_if_env_dirty()

    main_steady, tree_steady, samples = _measure_idle()
    assert samples, "未采集到任何有效内存样本（phys_footprint 读取失败或进程未起来）"
    assert tree_steady >= main_steady, (
        f"全树 {tree_steady:.0f}MB 小于主进程 {main_steady:.0f}MB：进程树遍历失效"
    )
    print(
        f"\n[内存预算·全树] 稳态={tree_steady:.0f}MB（主进程 {main_steady:.0f}MB，"
        f"子进程 {tree_steady - main_steady:.0f}MB）预算={TREE_BUDGET_MB:.0f}MB",
        flush=True,
    )
    assert tree_steady < TREE_BUDGET_MB, (
        f"空载稳态全树 {tree_steady:.0f}MB 超预算 {TREE_BUDGET_MB:.0f}MB"
        f"（主进程 {main_steady:.0f}MB + 子进程 {tree_steady - main_steady:.0f}MB）。"
        f"先复测基线再调 DRIFOX_MEM_TREE_BUDGET_MB。"
    )
