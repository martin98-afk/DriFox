# -*- coding: utf-8 -*-
"""启动里程碑预算门（H1）：真进程启动到首窗的绝对毫秒上限。

为什么需要它
------------
全仓此前没有任何测试测「真进程启动到首窗」的墙钟时间：
- `test_shell_first_boot.py` 只测 `preheat_process_level()`（实测 5ms vs 阈值 1500ms）
- `test_startup_init_lazy.py` 是源码文本匹配，改耗时不动结构则全绿
- `tests/perf` 曾被 `addopts --ignore` 整体排除

因此「有人把启动从 1000ms 改到 1500ms」在旧护栏下不会被任何测试发现。

口径
----
- 父进程墙钟：`subprocess.Popen` 时刻 → stderr 出现
  ``[StartupMark] first_chat_window`` 直写行。
- 该锚点含解释器冷启动与全部顶层 import（真端到端）。
- 每轮独立临时数据目录，轮前预热一轮（建库/写 pyc）丢弃，正式轮取中位数。

环境纪律
--------
测量型测试在脏环境下没有意义。前置检查不通过时 **skip 而不是 fail**：
宁可 skip，不要给一个假绿的预算门。
"""
from __future__ import annotations

import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ENTRY = _ROOT / "main.py"
_PY = sys.executable

# 与 app/utils/_startup_probe.py 的直写行格式一致（loguru 行带前缀，不匹配）
_MARK_RE = re.compile(r"^\[StartupMark\] (\S+) 耗时 (\d+)ms")

# 阈值：当前实测热启动中位 ~880ms（min 752 / max 917），留足余量。
# 纪律：宁松不紧 —— 频繁误红等于没有护栏。稳定运行一段时间后再逐步收紧。
STARTUP_BUDGET_MS = float(os.environ.get("DRIFOX_STARTUP_BUDGET_MS", "1500"))
ROUNDS = int(os.environ.get("DRIFOX_STARTUP_ROUNDS", "3"))

# 环境门：负载超过核数此比例、或已有 main.py 实例在跑 → skip
_LOAD_RATIO_MAX = 0.8

pytestmark = pytest.mark.perf


def _ancestor_pids(pid: int) -> set[int]:
    """当前进程的祖先链：其命令行可能含入口路径（pytest 调用链），必须排除。"""
    out: set[int] = set()
    cur = pid
    for _ in range(32):  # 防环
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
    """环境不干净就 skip：测量值不可信时给出假绿比不测更有害。"""
    ncpu = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = 0.0
    if load1 > ncpu * _LOAD_RATIO_MAX:
        pytest.skip(
            f"measurement env not clean: load1={load1:.2f} > {ncpu * _LOAD_RATIO_MAX:.1f} "
            f"({ncpu} cpus)"
        )

    # 已有真实实例会抢 CPU 与磁盘，且其子进程会污染计数。
    # 关键：必须排除 pytest 自身——其命令行里也含 main.py 路径，pgrep -f 会误判。
    me = os.getpid()
    ancestors = _ancestor_pids(me)
    try:
        proc = subprocess.run(
            ["pgrep", "-f", str(_ENTRY)],
            capture_output=True, text=True, timeout=10,
        )
        candidates = [int(x) for x in proc.stdout.split() if x.strip().isdigit()]
    except (subprocess.SubprocessError, ValueError):
        candidates = []
    others = [pid for pid in candidates if pid != me and pid not in ancestors]

    # 分级处置：
    # - 严格模式（DRIFOX_PERF_STRICT_ENV=1）：任何残留实例都 skip，适合 CI/基线采集
    # - 默认：仅警告。残留实例（尤其 PPID=1 的孤儿）在开发机上很常见，
    #   一律 skip 会让护栏长期静默 = 等于没有护栏。负载门仍会拦截真正不可信的环境。
    if others:
        msg = f"{len(others)} 个 main.py 实例残留: {others[:5]}"
        if os.environ.get("DRIFOX_PERF_STRICT_ENV") == "1":
            pytest.skip(f"measurement env not clean: {msg}")
        print(f"\n[warn] {msg}（非严格模式，继续测量）", flush=True)


def _run_once(data_dir: Path, exit_ms: int) -> float | None:
    """拉起一次真进程，返回 Popen→first_chat_window 墙钟毫秒。"""
    env = os.environ.copy()
    env.update(
        {
            "DRIFOX_STARTUP_PROBE": "1",
            "DRIFOX_STARTUP_PROBE_EXIT_MS": str(exit_ms),
            "DRIFOX_DATA_DIR": str(data_dir),
            "PYTHONPATH": str(_ROOT)
            + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
        }
    )
    # 必须真实渲染：offscreen 会改变首窗路径与耗时
    env.pop("QT_QPA_PLATFORM", None)

    t0 = time.perf_counter()
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
    first_window = None
    assert proc.stderr is not None
    try:
        # 读到目标打点即停止：不能依赖 EOF（进程在 EXIT_MS 到点后才退，
        # 而该定时器挂在探针 dump 之后；纯等 EOF 会撞 pytest 300s 超时）。
        while True:
            line = proc.stderr.readline()
            if not line:
                break
            if first_window is None:
                m = _MARK_RE.match(line)
                if m and m.group(1) == "first_chat_window":
                    first_window = (time.perf_counter() - t0) * 1000.0
                    break
    finally:
        # 不依赖 DRIFOX_STARTUP_PROBE_EXIT_MS 自退：该定时器挂在 dump() 之后，
        # 实测在部分路径不生效（子进程停在 Qt 事件循环不退出，成为孤儿并
        # 污染下一次测量的环境门）。测到打点后主动收尾。
        _stop_child(proc)
    return first_window


def _stop_child(proc: "subprocess.Popen[str]") -> None:
    """收尾被测子进程（含其 QtWebEngine 子进程）。幂等、失败静默。"""
    if proc.poll() is not None:
        return
    import signal as _signal

    try:
        proc.send_signal(_signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.send_signal(_signal.SIGKILL)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def _seed_data_dir() -> Path:
    """独立临时数据目录；复制真实 app.config 但不复制 sessions.db（保持首窗轻）。"""
    d = Path(tempfile.mkdtemp(prefix="drifox_startup_budget_"))
    for name in ("app.config",):
        src = _ROOT / ".drifox" / name
        if src.exists():
            shutil.copy2(src, d / name)
    return d


def test_startup_to_first_window_within_budget() -> None:
    """真进程启动到首窗中位数必须低于预算（默认 1500ms）。"""
    _skip_if_env_dirty()

    data_dir = _seed_data_dir()
    try:
        # 预热轮：首次运行会建库、写 pyc；不计入预算
        _run_once(data_dir, exit_ms=1500)

        samples: list[float] = []
        for _ in range(ROUNDS):
            ms = _run_once(data_dir, exit_ms=1500)
            if ms is not None:
                samples.append(ms)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)

    assert samples, (
        "未取到任何 first_chat_window 打点：启动链断裂或 DRIFOX_STARTUP_PROBE 失效"
    )
    median_ms = statistics.median(samples)
    print(
        f"\n[启动预算] {len(samples)} 轮 samples={[round(s) for s in samples]} "
        f"中位={median_ms:.0f}ms 预算={STARTUP_BUDGET_MS:.0f}ms",
        flush=True,
    )
    assert median_ms < STARTUP_BUDGET_MS, (
        f"启动到首窗中位 {median_ms:.0f}ms 超预算 {STARTUP_BUDGET_MS:.0f}ms"
        f"（samples={[round(s) for s in samples]}）。"
        f"若确认是可接受的增长，先复测基线再调 DRIFOX_STARTUP_BUDGET_MS，"
        f"不要为了变绿而放宽。"
    )


def test_startup_probe_emits_marks() -> None:
    """探针链路自检：必须能看到 first_chat_window 打点（防探针被静默移除）。"""
    _skip_if_env_dirty()
    data_dir = _seed_data_dir()
    try:
        ms = _run_once(data_dir, exit_ms=1200)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
    assert ms is not None, (
        "stderr 未出现 [StartupMark] first_chat_window："
        "启动探针链路失效（main.py 的 _smark 调用或 _startup_probe 被改动）"
    )
