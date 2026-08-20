# -*- coding: utf-8 -*-
"""基准 1+2：GUI 全链路启动耗时 + 启动后稳态内存

复刻 main.py 启动序列（QApplication → 资源注册 → OpenAIChatToolWindow →
TabManagerWindow.show），在隔离临时目录运行，QTimer 自动退出。

分阶段计时：
- t_interp      : 解释器启动到本脚本入口（main() 之前）
- t_import      : app.main_widget 导入耗时
- t_qapp        : QApplication 创建耗时
- t_window      : OpenAIChatToolWindow 构造耗时（含 ChatBackend.initialize 同步段）
- t_tm_show     : TabManagerWindow 构造+show 耗时（≈首帧可见）
- t_total_show  : python 启动 → 主窗口 show 总耗时（核心指标）

运行：
  uv run python benchmarks/bench_startup.py            # 跑 3 次取统计
  uv run python benchmarks/bench_startup.py --inner    # 单次内部运行（被外层调起）
  uv run python benchmarks/bench_startup.py --runs 5
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

# t0 必须在一切重导入之前
T0 = time.perf_counter()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_common as bc  # noqa: E402


def run_inner() -> dict:
    """单次 GUI 启动测量（本进程内）。"""
    tmp = bc.setup_isolation("startup")
    result = {"temp_dir": str(tmp)}

    t_interp = time.perf_counter() - T0
    result["t_interp_s"] = round(t_interp, 3)

    # tracemalloc 开在导入前，追踪导入期分配
    import tracemalloc

    tracemalloc.start(1)

    import warnings

    warnings.filterwarnings("ignore")
    os.environ.pop("QT_PLUGIN_PATH", None)

    # 与 main.py 同款：禁用 qFatal 默认 abort 行为，改为记录
    from loguru import logger
    from PyQt5.QtCore import QtMsgType, qInstallMessageHandler

    def _qt_message_handler(msg_type, msg_context, msg_text):
        if msg_type == QtMsgType.QtFatalMsg:
            logger.error(f"[QtFatal-bench] {msg_text}")
        elif msg_type == QtMsgType.QtCriticalMsg:
            logger.error(f"[QtCritical-bench] {msg_text}")

    qInstallMessageHandler(_qt_message_handler)

    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtWidgets import QApplication, QWidget

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    # WebEngine 必须在 QApplication 创建前导入（main.py 同款约束）
    t_i0 = time.perf_counter()
    import PyQt5.QtWebEngineWidgets  # noqa: F401

    t_web_import = time.perf_counter() - t_i0

    t_q0 = time.perf_counter()
    app = QApplication(sys.argv)
    t_qapp = time.perf_counter() - t_q0

    from app.utils import icons_rc, icons_light_rc  # noqa: F401

    app.setStyle("Fusion")
    app.setApplicationName("Drifox")

    # 主题
    try:
        from qfluentwidgets import Theme, setTheme

        setTheme(Theme.DARK)
    except Exception:
        pass

    # 主窗口构造（≈ ChatBackend.initialize 同步段）
    from app.main_widget import OpenAIChatToolWindow

    from app.utils.config import Settings

    class FakePage(QWidget):
        def __init__(self):
            super().__init__()
            self.cfg = Settings.get_instance()

        def isActiveWindow(self):
            return True

        @property
        def workflow_name(self):
            return "standalone_llm_chatter"

        @property
        def global_variables_changed(self):
            class FakeSignal:
                def connect(self, *a, **k):
                    pass

            return FakeSignal()

        def setUpdatesEnabled(self, e):
            pass

        def update(self):
            pass

        def show_splitter(self):
            pass

        def hide_splitter(self):
            pass

    t_w0 = time.perf_counter()
    fake_page = FakePage()
    chat_window = OpenAIChatToolWindow(fake_page)
    t_window = time.perf_counter() - t_w0

    # Tab 管理器模式 show（与 main.py 生产路径一致）
    t_s0 = time.perf_counter()
    from app.widgets.tab_manager_window import TabManagerWindow

    tm = TabManagerWindow.create_instance()
    tm.add_window(chat_window)
    tm.show()
    t_show_after = time.perf_counter()
    t_tm_show = t_show_after - t_s0
    t_total_show = t_show_after - T0

    result.update(
        {
            "t_web_import_ms": round(t_web_import * 1000, 1),
            "t_qapp_ms": round(t_qapp * 1000, 1),
            "t_window_ms": round(t_window * 1000, 1),
            "t_tm_show_ms": round(t_tm_show * 1000, 1),
            "t_total_show_s": round(t_total_show, 3),
        }
    )

    # ============ 稳态采样：等延迟组件（QTimer 0/200/400/600ms）跑完 ============
    state = {"samples": []}

    def _sample(tag):
        bc.full_gc()
        state["samples"].append(
            {
                "tag": tag,
                "rss_mb": round(bc.rss_mb(), 1),
                "private_mb": round(bc.private_mb(), 1),
                "tracemalloc_mb": round(bc.tracemalloc_current_mb(), 1),
            }
        )

    def _stage1():
        app.processEvents()
        _sample("show+1s")
        QTimer.singleShot(2500, _stage2)

    def _stage2():
        _sample("show+3.5s")
        QTimer.singleShot(2500, _stage3)

    def _stage3():
        _sample("show+6s")
        result["steady_samples"] = state["samples"]
        # tracemalloc Top20 大对象（稳态）
        result["tracemalloc_top20"] = bc.tracemalloc_top(20)
        # 对象计数统计（关键类型）
        try:
            from collections import Counter

            objs = gc.get_objects()
            counter = Counter(type(o).__module__ + "." + type(o).__name__ for o in objs)
            interesting = {}
            for key, cnt in counter.items():
                if any(
                    k in key
                    for k in (
                        "ChatSession",
                        "ChatBackend",
                        "MessageCard",
                        "dict",
                        "QObject",
                        "QTimer",
                        "Worker",
                    )
                ):
                    interesting[key] = cnt
            result["object_counts"] = dict(
                sorted(interesting.items(), key=lambda kv: -kv[1])[:15]
            )
        except Exception as e:
            result["object_counts_error"] = str(e)
        finally:
            objs = None  # 释放引用（try 内 gc.get_objects 失败时不 del 未绑定名）
        print("[BENCH_RESULT]" + json.dumps(result, ensure_ascii=False))
        app.quit()

    # [PERF] 复刻 main.py DeferredStartup：初始化共享 WebEngine Profile
    # （懒加载 batch 渲染欢迎卡片 CodeWebViewer 依赖它，缺失会 qFatal）
    try:
        from app.core.webengine_profile import init_shared_web_profile

        init_shared_web_profile(parent=app)
    except Exception:
        logger.exception("[BENCH] init_shared_web_profile 失败")

    QTimer.singleShot(1000, _stage1)
    app.exec_()

    # 清理（走 backend cleanup 释放路径）
    try:
        chat_window.backend.cleanup()
    except Exception:
        pass
    return result


def run_outer(runs: int) -> dict:
    """子进程跑 N 次，汇统计计。"""
    import statistics

    all_runs = []
    for i in range(runs):
        print(f"\n===== 启动基准 run {i + 1}/{runs} =====")
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--inner"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        got = None
        for line in proc.stdout.splitlines():
            if line.startswith("[BENCH_RESULT]"):
                got = json.loads(line[len("[BENCH_RESULT]") :])
                break
        if not got:
            print("STDERR tail:")
            print("\n".join(proc.stderr.splitlines()[-30:]))
            print("STDOUT tail:")
            print("\n".join(proc.stdout.splitlines()[-30:]))
            raise RuntimeError(f"run {i + 1} 未产出结果，exit={proc.returncode}")
        all_runs.append(got)
        print(
            f"  total_show={got['t_total_show_s']}s "
            f"window={got['t_window_ms']}ms rss_steady={got['steady_samples'][-1]['rss_mb']}MB"
        )

    totals = [r["t_total_show_s"] for r in all_runs]
    windows = [r["t_window_ms"] for r in all_runs]
    steady_rss = [r["steady_samples"][-1]["rss_mb"] for r in all_runs]
    steady_tm = [r["steady_samples"][-1]["tracemalloc_mb"] for r in all_runs]

    summary = {
        "metric": "startup",
        "runs": runs,
        "t_total_show_s": {
            "min": min(totals),
            "median": round(statistics.median(totals), 3),
            "max": max(totals),
        },
        "t_window_ms": {
            "min": min(windows),
            "median": round(statistics.median(windows), 1),
            "max": max(windows),
        },
        "steady_rss_mb": {
            "min": min(steady_rss),
            "median": round(statistics.median(steady_rss), 1),
            "max": max(steady_rss),
        },
        "steady_tracemalloc_mb": {
            "min": min(steady_tm),
            "median": round(statistics.median(steady_tm), 1),
            "max": max(steady_tm),
        },
        "detail_last_run": all_runs[-1],
    }
    print("\n===== 启动基准汇总 =====")
    print(json.dumps({k: v for k, v in summary.items() if k != "detail_last_run"}, ensure_ascii=False, indent=2))
    bc.save_result("startup", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inner", action="store_true")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.inner:
        run_inner()
    else:
        run_outer(args.runs)
