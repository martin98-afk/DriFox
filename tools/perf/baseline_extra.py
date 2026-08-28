# -*- coding: utf-8 -*-
"""
DriFox 性能基线扩展脚本（PySide6 GUI）— 补充 baseline.py 未覆盖的维度

覆盖 leader 子任务 #3 重点指标缺口：
  A) 1000 项 QListWidget 滚动平均帧率（交互态，列表已加载、事件循环空闲后驱动）
  B) 100 次 QStackedWidget 切页 / 列表刷新 平均耗时 + P95 + CPU 峰值
  C) 上述操作期间的 CPU 峰值（psutil 周期采样 process.cpu_percent）
  D) 长时运行 RSS 增长曲线（稳定可复现，无 WebEngine 干扰，框架级趋势）

设计原则（同 baseline.py）：
  - 不修改任何产品代码（app/ 未触碰），仅运行时构造标准 Qt 控件。
  - 仅依赖标准库 + psutil（项目已有）。
  - 默认 offscreen 平台，无显示器可复现；真实渲染帧率可用 --platform windows。
  - 控件级基准：用标准 Qt 控件（QListWidget / QStackedWidget）反映框架层交互开销；
    DriFox 具体业务控件性能需结合 #1 诊断 + 后续优化。

运行：
  .venv\Scripts\python.exe tools\perf\baseline_extra.py all --repeats 3 --out tools/perf/results/extra_before.json
  .venv\Scripts\python.exe tools\perf\baseline_extra.py scroll --items 1000 --duration-ms 3000
  .venv\Scripts\python.exe tools\perf\baseline_extra.py nav --ops 100 --pages 8
  .venv\Scripts\python.exe tools\perf\baseline_extra.py longevity --ops 4000 --op-interval-ms 50
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import subprocess
import sys
import threading
import time

import psutil

# ---- Qt 平台：默认 offscreen 以便无显示器复现 ----
if "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtCore import QTimer, Qt, QMetaObject, QElapsedTimer
from PySide6.QtWidgets import (QApplication, QMainWindow, QListWidget, QListWidgetItem,
                             QStackedWidget, QLabel)

SELF = os.path.abspath(__file__)


def _stdev(xs):
    return round(statistics.pstdev(xs), 4) if len(xs) > 1 else 0.0


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _meta(platform):
    import PySide6.QtCore as QtCore
    from datetime import datetime, timezone
    return {
        "tool": "DriFox baseline_extra.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "qt_version": QtCore.QT_VERSION_STR,
        "platform": platform,
        "machine": sys.platform,
        "note": "控件级基准：标准 Qt 控件（QListWidget/QStackedWidget），反映