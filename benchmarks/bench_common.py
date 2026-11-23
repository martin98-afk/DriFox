# -*- coding: utf-8 -*-
"""基准测试公共工具

所有基准脚本共用：
- 数据隔离：开发环境 get_app_data_dir() = cwd/.drifox（含 sessions.db），
  基准运行前 chdir 到独立临时目录，避免污染真实用户数据。
- 采样：psutil RSS + tracemalloc
- 分析：线性回归斜率、Top-N 大对象

运行方式：uv run python benchmarks/bench_xxx.py
"""

from __future__ import annotations

import gc
import json
import os
import platform
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 确保可导入项目包（无论从哪个 cwd 启动）
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 基准结果输出目录（benchmarks/results/，不进 git 数据库）
RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def real_python() -> str:
    """返回不含 launcher 中间层的真解释器路径。

    uv 建的 venv 里 `Scripts/python.exe` 是 ~241KB 的 shim：它先起一个极小的
    父进程，再 spawn 真正的 CPython。直接用 `sys.executable` 拉子进程会多出
    一层父进程，后果有两处：
      1) 进程树 RSS 分类失真（父进程占 main，真解释器落到 python_child）；
      2) 启动耗时被 launcher 层放大（实测 t_interp 0.165s → 16.4s）。
    故优先用 `sys.base_prefix/python.exe`（venv 的 base 解释器）。解析失败
    回退 `sys.executable`。

    注意：真解释器不执行 venv 的 `*.pth`，仅靠 PYTHONPATH 会让 pywin32 报
    `DLL load failed`（pywintypes314.dll 在 pywin32_system32/）。故配套用
    :func:`spawn_env` 注入一个 sitecustomize 垫片，见其说明。
    """
    try:
        base = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python")
        if base.exists() and str(base) != sys.executable:
            return str(base)
    except Exception:
        pass
    return sys.executable


def site_packages() -> str:
    """当前解释器的首个 site-packages（真解释器场景注入搜索路径用）。"""
    for p in sys.path:
        if p and ("site-packages" in p or "dist-packages" in p):
            return p
    return ""


_SITECUSTOMIZE = '''"""基准子进程垫片：把 uv venv 的搜索路径与 DLL 目录补齐到真解释器。"""
import os, sys

_sp = {site_packages!r}
if _sp and _sp not in sys.path:
    sys.path.insert(0, _sp)

# venv 的 pywin32.pth 内容（真解释器不会执行 pth，必须在此显式重放）
for _sub in ("win32", "win32/lib", "pythonwin"):
    _p = os.path.join(_sp, _sub.replace("/", os.sep))
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)

# pywintypes314.dll / pythoncom314.dll 只靠 PATH 找不到（win32api.pyd 运行时依赖）
_dll = os.path.join(_sp, "pywin32_system32")
if os.path.isdir(_dll):
    try:
        os.add_dll_directory(_dll)
    except Exception:
        pass
    os.environ["PATH"] = _dll + os.pathsep + os.environ.get("PATH", "")

# editable 安装：__editable__*.pth 里的 finder 同样需要手动重放
for _n in os.listdir(_sp) if os.path.isdir(_sp) else []:
    if _n.startswith("__editable__") and _n.endswith(".pth"):
        try:
            exec(open(os.path.join(_sp, _n), encoding="utf-8").read())
        except Exception:
            pass
            
# 让子进程也能找到仓库根（benchmarks 脚本 import app.*）
_root = {root!r}
if _root and _root not in sys.path:
    sys.path.insert(0, _root)
'''


def spawn_env(extra: dict | None = None) -> dict:
    """子进程环境：真解释器场景下补 site-packages 路径与 pywin32 DLL 目录。

    做法：先写一个临时 `sitecustomize.py` 到独立目录，把该目录放到
    PYTHONPATH 最前。Python 启动时自动 import sitecustomize，垫片里重放
    venv 的 `pywin32.pth` / `__editable__*.pth` 内容并注册 DLL 目录。
    这比裸 PYTHONPATH 干净：`win32api` 的 DLL 依赖只有 `add_dll_directory`
    能解，单靠 PATH 不够（已实测确认）。
    """
    env = dict(os.environ)
    exe = real_python()
    if exe != sys.executable:
        sp = site_packages()
        if sp:
            shim_dir = Path(tempfile.gettempdir()) / "drifox_bench_shim"
            try:
                shim_dir.mkdir(parents=True, exist_ok=True)
                (shim_dir / "sitecustomize.py").write_text(
                    _SITECUSTOMIZE.format(
                        site_packages=sp, root=str(PROJECT_ROOT)
                    ),
                    encoding="utf-8",
                )
                prev = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = str(shim_dir) + (
                    os.pathsep + prev if prev else ""
                )
            except Exception:
                # 写垫片失败也不能让基准崩，退化到裸 PYTHONPATH
                env["PYTHONPATH"] = sp + (
                    os.pathsep + env.get("PYTHONPATH") if env.get("PYTHONPATH") else ""
                )
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def setup_isolation(tag: str) -> Path:
    """chdir 到独立临时目录，隔离 .drifox 用户数据（sessions.db/插件/配置）。

    必须在任何 app.* 模块导入【之前】调用（get_app_data_dir 按需解析，
    SessionStore/PluginManager 单例首次 get_instance 时才锁定路径）。
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"drifox_bench_{tag}_"))
    os.chdir(tmp)
    (tmp / ".drifox").mkdir(exist_ok=True)
    return tmp


def env_info() -> dict:
    """采集运行环境（保证可复现）。"""
    import psutil

    return {
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpu": platform.processor() or f"{os.cpu_count()} cores",
        "cpu_count": os.cpu_count(),
        "mem_total_gb": round(psutil.virtual_memory().total / 1024**3, 1),
        "cwd": os.getcwd(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def rss_mb() -> float:
    """当前进程 RSS（MB）。"""
    import psutil

    return psutil.Process().memory_info().rss / 1024**2


def private_mb() -> float:
    """当前进程私有内存（Windows: privatebytes / 其他: USS 可得时）。"""
    import psutil

    try:
        m = psutil.Process().memory_full_info()
        v = getattr(m, "uss", None) or getattr(m, "private", None)
        if v:
            return v / 1024**2
    except Exception:
        pass
    return rss_mb()


def full_gc(rounds: int = 2):
    """彻底 GC（含分代 + finalize）。"""
    for _ in range(rounds):
        gc.collect()


def tracemalloc_top(n: int = 20) -> list:
    """tracemalloc 当前 Top-N 分配点（按累计大小）。"""
    snap = tracemalloc.take_snapshot()
    out = []
    for stat in snap.statistics("lineno")[:n]:
        frame = stat.traceback[0]
        out.append(
            {
                "loc": f"{Path(frame.filename).name}:{frame.lineno}",
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
            }
        )
    return out


def tracemalloc_current_mb() -> float:
    cur, _peak = tracemalloc.get_traced_memory()
    return cur / 1024**2


def tracemalloc_diff(snap1, snap2, n: int = 15) -> list:
    """两个 snapshot 对比：新增分配 Top-N（按增长字节，过滤负增长）。"""
    diffs = snap2.compare_to(snap1, "lineno")
    out = []
    for stat in diffs[: 3 * n]:
        if stat.size <= 0:
            continue
        frame = stat.traceback[0]
        out.append(
            {
                "loc": f"{Path(frame.filename).name}:{frame.lineno}",
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
            }
        )
        if len(out) >= n:
            break
    return out


def slope(xs: list, ys: list) -> tuple:
    """最小二乘线性回归：返回 (斜率, R²)。"""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0:
        return 0.0, 0.0
    b = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 1.0
    return b, r2


def save_result(name: str, data: dict):
    """结果落盘 benchmarks/results/<name>_<标签>.json。

    [Y2] 防同名覆盖：标签取 set_result_tag 设置的 --tag 可选举；未传时
    自动加时间戳后缀（YYYYmmdd_HHMMSS），同一天多次跑不再互相覆盖。
    """
    data.setdefault("_env", env_info())
    suffix = _RESULT_TAG or time.strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"{name}_{suffix}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[bench] 结果已写入 {path}")
    return path

_RESULT_TAG: str | None = None

def set_result_tag(tag: str | None) -> None:
    """[Y2] 设置结果文件可选举名（来自 --tag）；None 则 save_result 用时间戳。"""
    global _RESULT_TAG
    _RESULT_TAG = tag

def warn_if_drifox_running(tag: str) -> None:
    """[Y1] 已有 Drifox 实例在跑时告警（不改行为，仅提示数字可能失真）。

    匹配模式：cmdline 含 main.py + PROJECT_ROOT，或进程名含 drifox（大小写
    不敏感，覆盖打包版 Drifox.app / Drifox.exe）。
    """
    import psutil
    me = os.getpid()
    hits: list[int] = []
    root_s = str(PROJECT_ROOT)
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        if p.info["pid"] == me:
            continue
        cl = " ".join(p.info["cmdline"] or [])
        nm = (p.info["name"] or "").lower()
        if ("main.py" in cl and root_s in cl) or "drifox" in nm:
            hits.append(p.info["pid"])
    if hits:
        print(f"[WARN] 检测到 {len(hits)} 个疑似 Drifox 实例在跑 (pids={hits})："
              "基线数字可能受资源竞争/共享 .drifox 干扰，建议关闭实例或确认隔离后"
              "再采信本次结果", flush=True)


def spin_qt_events(qapp, total_ms: int, step_ms: int = 50):
    """在无 exec_() 的情况下泵 Qt 事件循环 total_ms 毫秒（驱动 QTimer 延迟任务）。"""
    end = time.perf_counter() + total_ms / 1000
    while time.perf_counter() < end:
        qapp.processEvents()
        remaining = end - time.perf_counter()
        time.sleep(min(step_ms / 1000, max(remaining, 0)))


def leak_verdict(kb_per_iter: float, r2: float, threshold_kb: float = 10.0) -> str:
    """泄漏判定：斜率显著为正 → 泄漏嫌疑。"""
    if kb_per_iter > threshold_kb and r2 > 0.7:
        return f"疑似泄漏：+{kb_per_iter:.1f} KB/次 (R²={r2:.3f})"
    if kb_per_iter > threshold_kb:
        return f"增长但线性度低：+{kb_per_iter:.1f} KB/次 (R²={r2:.3f})，可能为缓存填充"
    return f"无显著泄漏：{kb_per_iter:+.1f} KB/次 (R²={r2:.3f})"
