# -*- coding: utf-8 -*-
"""内存快照诊断工具（DriFox 窗口 / 附件 / WebEngine 生命周期排查）。

为次波压测 / 快照分析提供一站式内存采集能力，四类信号互补：
- tracemalloc  ：Python 堆分配热点（top 行 + 与上一份快照的增量）
- psutil       ：进程 RSS（物理内存，MB）
- pympler ClassTracker：关键类存活实例数与累计字节（构造函数级埋点）
- objgraph     ：关键类存活实例计数（基于 GC 对象图，独立于 ClassTracker）

公开 API
--------
- start()               尽早调用，启动 tracemalloc 并注册 ClassTracker 追踪目标类
- snapshot(label)       采集一次快照，返回结构化 dict（不触发业务、不创建窗口）
- diff(before, after)   对比两次快照，返回增量 dict
- print_snapshot(snap)  打印快照（控制台）
- print_diff(d)         打印增量
- self_check()          校验 TRACK_CLASSES 导入路径是否可解析（离线 / offscreen）

设计要点
--------
- 顶层只 import 标准库 + psutil/pympler/objgraph（均已装入 .venv），**不 import 任何业务模块**，
  因此 `import tools.mem_snapshot` 在纯 Python 环境下即可通过，不会触发业务代码或创建窗口。
- 业务类（OpenAIChatToolWindow / AttachmentChip / QWebEngineView）在首次 snapshot / start 时
  **惰性导入**，导入路径经 grep 校准（见 TRACK_CLASSES 注释）。导入失败抛清晰错误。
- 为避免 `QtWebEngineWidgets must be imported before a QCoreApplication instance is created`，
  惰性导入前显式导入 PyQt5 WebEngine 作兜底（在已初始化的 app 进程中该前提已满足）。

集成点（业务代码插入留待修复阶段，本脚本不修改任何业务代码）：
- tools/mem_snapshot 已在 add_window / _close_window_at 等生命周期边界预留调用位置，
  调用方只需 `from tools.mem_snapshot import snapshot` 后按 label 取点即可。
"""
from __future__ import annotations

import os
import sys
import tracemalloc
from typing import Any, Dict, List, Optional, Tuple

# ── 诊断库（均为项目 .venv 已装依赖；import 失败给出明确安装提示）──
try:
    import psutil
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "mem_snapshot 需要 psutil，请执行: "
        "uv pip install --python .venv/Scripts/python.exe psutil"
    ) from exc

try:
    from pympler.classtracker import ClassTracker
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "mem_snapshot 需要 pympler，请执行: "
        "uv pip install --python .venv/Scripts/python.exe pympler"
    ) from exc

try:
    import objgraph
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "mem_snapshot 需要 objgraph，请执行: "
        "uv pip install --python .venv/Scripts/python.exe objgraph"
    ) from exc


# ── 追踪目标（导入路径经 grep 校准于 D:/work/DriFox）──
# OpenAIChatToolWindow -> app/main_widget.py:1008   class OpenAIChatToolWindow(ToolWindow)
# AttachmentChip       -> app/widgets/bottom_input_area.py:1927   class AttachmentChip(QFrame)
# QWebEngineView       -> PyQt5.QtWebEngineWidgets（Qt 内置，非业务类，用于观察 WebView 实例堆积）
TRACK_CLASSES: List[Tuple[str, str]] = [
    ("app.main_widget", "OpenAIChatToolWindow"),
    ("app.widgets.bottom_input_area", "AttachmentChip"),
    ("PyQt5.QtWebEngineWidgets", "QWebEngineView"),
]

_TRACE_FRAMES = 25   # tracemalloc 栈帧保留深度
_TOP_N = 15          # 统计展示的 top 行数

_TRACKER: Optional[ClassTracker] = None
_STARTED = False
_LAST_TM_SNAPSHOT = None  # 上一份 tracemalloc Snapshot，用于增量对比


def _ensure_project_root() -> None:
    """将项目根加入 sys.path，使 `app.*` 业务模块可被惰性导入。

    覆盖两种运行方式：
    - `uv run python -c "import tools.mem_snapshot"`（cwd=项目根，已可解析）
    - `python tools/mem_snapshot.py`（sys.path[0]=tools/，需回退到上一级）
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if root not in sys.path:
        sys.path.insert(0, root)


def _resolve_track_classes() -> List[type]:
    """惰性导入追踪目标类，返回类对象列表。

    业务模块（app.main_widget 等）在 import 时不应创建窗口；为避免
    'QtWebEngineWidgets must be imported before a QCoreApplication instance is created'，
    先确保 PyQt5 WebEngine 已被导入（在已初始化的 app 进程中该前提通常已满足）。
    """
    _ensure_project_root()
    try:
        from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except Exception:
        # 纯离线 / 非 Qt 环境（仅 import 验证）下 WebEngine 解析失败可接受，仅跳过该兜底
        pass
    classes: List[type] = []
    for module_name, class_name in TRACK_CLASSES:
        try:
            mod = __import__(module_name, fromlist=[class_name])
            classes.append(getattr(mod, class_name))
        except Exception as exc:
            raise ImportError(
                f"无法解析追踪目标 {module_name}.{class_name}: {exc}"
            ) from exc
    return classes


def start() -> None:
    """启动诊断：开启 tracemalloc 并注册 ClassTracker 目标类。

    应在进程早期、目标类被实例化之前调用（例如在 main 入口 QApplication 创建后、
    首个窗口构建前）。重复调用安全（幂等）。
    """
    global _TRACKER, _STARTED
    if _STARTED:
        return
    if not tracemalloc.is_tracing():
        tracemalloc.start(_TRACE_FRAMES)
    _TRACKER = ClassTracker()
    for cls in _resolve_track_classes():
        _TRACKER.track_class(cls, name=cls.__name__)
    _STARTED = True


def _traceback_str(traceback) -> str:
    # tracemalloc Statistic.traceback 是 Frame 序列（filename/lineno），
    # 注意：不要调用 .format()（其返回字符串列表），直接遍历 Frame 对象。
    return " -> ".join(
        f"{os.path.basename(f.filename)}:{f.lineno}" for f in traceback
    )


def snapshot(label: str) -> Dict[str, Any]:
    """采集一次内存快照，返回结构化 dict。

    不触发业务代码、不创建窗口。首次调用自动 start()。
    """
    global _LAST_TM_SNAPSHOT
    if not _STARTED:
        start()

    # 1) psutil RSS（物理内存）
    rss_mb = psutil.Process().memory_info().rss / (1024.0 * 1024.0)

    # 2) tracemalloc：当前快照统计 + 与上一份快照的增量
    tm_snap = tracemalloc.take_snapshot()
    top_stats = tm_snap.statistics("lineno")[:_TOP_N]
    top = [
        {
            "size_kb": stat.size / 1024.0,
            "count": stat.count,
            "trace": _traceback_str(stat.traceback),
        }
        for stat in top_stats
    ]
    current_mb = sum(s.size for s in tm_snap.statistics("filename")) / (1024.0 * 1024.0)
    delta = None
    if _LAST_TM_SNAPSHOT is not None:
        diff_stats = tm_snap.compare_to(_LAST_TM_SNAPSHOT, "lineno")[:_TOP_N]
        delta = [
            {
                "size_kb": st.size / 1024.0,
                "count": st.count,
                "trace": _traceback_str(st.traceback),
            }
            for st in diff_stats
        ]
    _LAST_TM_SNAPSHOT = tm_snap

    # 3) pympler ClassTracker：关键类存活实例数 / 累计创建数
    #    pympler 的 Snapshot / ConsoleStats 不直接暴露类级聚合，这里遍历
    #    tracker.index（class_name -> [TrackedObject]），按弱引用是否仍存活判断。
    _TRACKER.create_snapshot(label)
    tracker_stats: Dict[str, Dict[str, Any]] = {}
    for cls_name, tobjs in _TRACKER.index.items():
        alive = 0
        for tobj in tobjs:
            ref = tobj.ref
            inst = ref() if callable(ref) else ref
            if inst is not None:
                alive += 1
        tracker_stats[cls_name] = {"alive": alive, "total": len(tobjs)}

    # 4) objgraph：关键类存活实例计数（基于 GC 对象图，独立于 ClassTracker）
    obj_counts: Dict[str, int] = {}
    for _module_name, class_name in TRACK_CLASSES:
        try:
            obj_counts[class_name] = objgraph.count(class_name)
        except Exception:
            obj_counts[class_name] = -1  # 该类未解析 / 无可统计实例

    return {
        "label": label,
        "rss_mb": rss_mb,
        "tracemalloc": {
            "current_mb": current_mb,
            "top": top,
            "delta": delta,
        },
        "tracker": tracker_stats,
        "objgraph_counts": obj_counts,
    }


def diff(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """对比两次快照，返回增量 dict（after - before）。"""
    d_tracker: Dict[str, Dict[str, Any]] = {}
    t_keys = set(before.get("tracker", {})) | set(after.get("tracker", {}))
    for k in sorted(t_keys):
        a = before.get("tracker", {}).get(k, {})
        b = after.get("tracker", {}).get(k, {})
        d_tracker[k] = {
            "alive_delta": b.get("alive", 0) - a.get("alive", 0),
            "total_delta": b.get("total", 0) - a.get("total", 0),
        }

    d_obj: Dict[str, int] = {}
    o_keys = set(before.get("objgraph_counts", {})) | set(after.get("objgraph_counts", {}))
    for k in sorted(o_keys):
        a = before.get("objgraph_counts", {}).get(k, 0) or 0
        b = after.get("objgraph_counts", {}).get(k, 0) or 0
        d_obj[k] = b - a

    return {
        "label_before": before.get("label"),
        "label_after": after.get("label"),
        "rss_mb_delta": round(after.get("rss_mb", 0.0) - before.get("rss_mb", 0.0), 2),
        "tracker": d_tracker,
        "objgraph_counts_delta": d_obj,
    }


def print_snapshot(snap: Dict[str, Any]) -> None:
    """将一次快照打印到 stdout（紧凑易读）。"""
    print(f"=== snapshot [{snap['label']}] ===")
    print(f"RSS: {snap['rss_mb']:.2f} MB | tracemalloc current: "
          f"{snap['tracemalloc']['current_mb']:.2f} MB")
    print("-- pympler ClassTracker (存活实例 / 累计字节) --")
    for name, s in snap["tracker"].items():
        print(f"  {name}: alive={s['alive']} total={s['total']}")
    print("-- objgraph 存活实例计数 --")
    for name, n in snap["objgraph_counts"].items():
        print(f"  {name}: {n}")
    if snap["tracemalloc"]["delta"] is not None:
        print("-- tracemalloc 增量 (top, 相对上一份快照) --")
        for st in snap["tracemalloc"]["delta"]:
            print(f"  +{st['size_kb']:.2f} KB x{st['count']}  {st['trace']}")


def print_diff(d: Dict[str, Any]) -> None:
    """将增量快照打印到 stdout。"""
    print(f"=== diff [{d['label_before']}] -> [{d['label_after']}] ===")
    print(f"RSS delta: {d['rss_mb_delta']:+.2f} MB")
    print("-- ClassTracker 存活实例增量 --")
    for name, s in d["tracker"].items():
        print(f"  {name}: alive {s['alive_delta']:+d}  total {s['total_delta']:+d}")
    print("-- objgraph 存活实例增量 --")
    for name, n in d["objgraph_counts_delta"].items():
        print(f"  {name}: {n:+d}")


def self_check() -> bool:
    """校验 TRACK_CLASSES 导入路径是否可解析。

    在 offscreen 平台下尝试惰性导入业务类，用于离线确认路径正确。
    成功返回 True；任一路径不可解析抛出 ImportError。
    """
    classes = _resolve_track_classes()
    print(f"[self_check] 成功解析 {len(classes)} 个追踪目标:")
    for (mod, name), cls in zip(TRACK_CLASSES, classes):
        print(f"  {mod}.{name} -> {cls!r}")
    return True


if __name__ == "__main__":
    # 离线自检：尽量在 offscreen 下验证导入路径（不触发业务、不创建窗口）。
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QApplication
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
        from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except Exception as e:  # pragma: no cover
        print(f"[self_check] Qt/WebEngine 预导入跳过（非 Qt 环境）: {e}")
    ok = self_check()
    print(f"[self_check] 结果: {'PASS' if ok else 'FAIL'}")
