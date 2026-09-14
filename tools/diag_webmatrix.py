# -*- coding: utf-8 -*-
"""WebEngine 渲染参数矩阵探针 —— 在纯 WebView 路线下找内存的最优参数组合。

与 diag_webengine_mem_probe.py 的区别：
  前者固定一组参数，测「每 view 增量」；
  本脚本跑**参数矩阵**，重点回答三个决策问题：
    Q1. --renderer-process-limit 从 6 降到 3 / 2，12 个 view 能省多少？
    Q2. 卡片逻辑高度（MAX_HEIGHT 语义）从 2000 降到 1200 / 800，合成表面能省多少？
    Q3. 池化空闲实例（MAX_IDLE_PER_BUCKET 语义）常驻成本是多少？

每个组合在**独立子进程**里跑（Chromium 只在进程首次初始化时读环境变量，
同一进程内无法切换 --renderer-process-limit），主进程负责汇总。

用法：
    python tools/diag_webmatrix.py                       # 全矩阵（约 8-12 分钟）
    python tools/diag_webmatrix.py --views 12            # 只测 12 页
    python tools/diag_webmatrix.py --quick               # 精简矩阵（3 组，约 3 分钟）

输出：markdown 表格 + JSON（--json 落盘）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CHILD = os.path.join(_HERE, "_diag_webmatrix_child.py")


def _build_child() -> str:
    """生成子进程脚本（一次性写入，避免手工维护第二个文件）。"""
    code = r'''
# -*- coding: utf-8 -*-
"""diag_webmatrix 的子进程探针：单组参数 → RSS 增量 JSON。"""
import argparse, os, sys, time

def _parse():
    p = argparse.ArgumentParser()
    p.add_argument("--views", type=int, default=12)
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--backend", default="software",
                   choices=["software", "hardware", "swiftshader"])
    p.add_argument("--width", type=int, default=700)
    p.add_argument("--height", type=int, default=2000)
    p.add_argument("--idle", type=int, default=0, help="额外常驻的隐藏 view 数（模拟池化空闲实例）")
    p.add_argument("--settle", type=float, default=1.0)
    return p.parse_args()

A = _parse()

os.environ["QT_OPENGL"] = "angle"
os.environ["QT_ANGLE_PLATFORM"] = "d3d11" if A.backend == "hardware" else "warp"
# 与 render_env.build_chromium_flags 的 GPU 段对齐：
#   software（默认档）：--disable-gpu --disable-software-rasterizer（禁 GPU 进程）
#   hardware：保留 GPU 进程 + --disable-gpu-compositing，禁软件光栅
#   swiftshader：保留 GPU 进程 + --use-angle=swiftshader
if A.backend == "hardware":
    _gpu_seg = "--disable-gpu-compositing --disable-software-rasterizer"
elif A.backend == "swiftshader":
    _gpu_seg = "--enable-unsafe-swiftshader --use-angle=swiftshader"
else:
    _gpu_seg = "--disable-gpu --disable-software-rasterizer"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    f"--renderer-process-limit={A.limit} {_gpu_seg}"
    " --disable-dev-shm-usage --enable-low-end-device-mode --disable-extensions"
)
for _k in ("QSG_RHI", "QSG_RHI_BACKEND"):
    os.environ.pop(_k, None)

import psutil
from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWebEngineWidgets import QWebEngineView

QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
QApplication.setAttribute(Qt.AA_UseOpenGLES)

_MB = 1024.0 * 1024.0
_proc = psutil.Process()


def rss_mb():
    return _proc.memory_info().rss / _MB


def child_rss():
    total = 0.0
    detail = {}
    try:
        kids = _proc.children(recursive=True)
    except Exception:
        return total, detail
    for k in kids:
        try:
            cmd = " ".join(k.cmdline() or [])
            r = k.memory_info().rss / _MB
        except Exception:
            continue
        if "QtWebEngineProcess" not in cmd and "qwebengine" not in cmd.lower():
            continue
        typ = "other"
        for m in ("--type=gpu", "--type=renderer", "--type=utility", "--type=zygote", "--type=broker"):
            if m in cmd:
                typ = m.split("=", 1)[1]
                break
        detail[typ] = detail.get(typ, 0.0) + r
        total += r
    return total, detail


def pump(sec):
    app = QApplication.instance()
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


app = QApplication(sys.argv)

HTML = """
<html><body style="margin:0;padding:12px;font:14px/1.6 system-ui;background:#fff">
<h3>msg {i}</h3>
<pre style="background:#f6f8fa;padding:10px;border-radius:6px"><code>
def example(items):
    return [x for x in items if x]
</code></pre>
<p>{i} — 模拟真实卡片正文：中英文混排、代码块、列表。</p>
<ul><li>条目一</li><li>条目二</li></ul>
</body></html>
"""

# 预热：让 browser/utility 进程先起来，避免把它算进增量
warm = QWebEngineView()
warm.resize(200, 200)
warm.setHtml("<html><body></body></html>", QUrl("about:blank"))
pump(1.2)
warm.deleteLater()
pump(0.8)

base_main = rss_mb()
base_child, _ = child_rss()

views = []
for i in range(A.views):
    v = QWebEngineView()
    v.resize(A.width, A.height)
    v.setHtml(HTML.format(i=i), QUrl("about:blank"))
    views.append(v)
    pump(A.settle)

# 额外常驻的隐藏 view（模拟 WebViewPool 空闲桶）
idle_views = []
for i in range(A.idle):
    v = QWebEngineView()
    v.resize(A.width, A.height)
    v.setAttribute(Qt.WA_DontShowOnScreen, True)
    v.setHtml(HTML.format(i=f"idle{i}"), QUrl("about:blank"))
    idle_views.append(v)
    pump(A.settle)

end_main = rss_mb()
end_child, detail = child_rss()

print("@@RESULT@@" + __import__("json").dumps({
    "views": A.views,
    "limit": A.limit,
    "backend": A.backend,
    "height": A.height,
    "idle": A.idle,
    "main_delta_mb": round(end_main - base_main, 1),
    "child_delta_mb": round(end_child - base_child, 1),
    "total_delta_mb": round((end_main - base_main) + (end_child - base_child), 1),
    "child_detail": {k: round(v, 1) for k, v in detail.items()},
}))
'''
    with open(_CHILD, "w", encoding="utf-8", newline="") as f:
        f.write(code)
    return _CHILD


def _run_child(child: str, views: int, limit: int, height: int, idle: int, settle: float, timeout: int,
               backend: str = "software"):
    cmd = [
        sys.executable, child,
        "--views", str(views),
        "--limit", str(limit),
        "--backend", backend,
        "--height", str(height),
        "--idle", str(idle),
        "--settle", str(settle),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None
    for line in out.stdout.splitlines():
        if line.startswith("@@RESULT@@"):
            try:
                return json.loads(line[len("@@RESULT@@"):])
            except Exception:
                return None
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="DriFox WebEngine 渲染参数矩阵探针")
    ap.add_argument("--views", type=int, default=12, help="每组创建的可见 view 数（默认 12）")
    ap.add_argument("--quick", action="store_true", help="精简矩阵（3 组）")
    ap.add_argument("--settle", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=180, help="每组超时秒数")
    ap.add_argument("--json", default="", help="结果落盘路径")
    args = ap.parse_args()

    child = _build_child()

    # combos: (backend, limit, height, idle)
    if args.quick:
        combos = [
            ("software", 6, 2000, 0),   # 出厂基准
            ("software", 2, 2000, 0),
            ("hardware", 1, 2000, 0),   # 用户当前配置
        ]
    else:
        combos = [
            ("software", 6, 2000, 0),   # 出厂基准（--disable-gpu）
            ("software", 3, 2000, 0),
            ("software", 2, 2000, 0),
            ("hardware", 6, 2000, 0),   # 保留 GPU 进程
            ("hardware", 3, 2000, 0),
            ("hardware", 2, 2000, 0),
            ("hardware", 1, 2000, 0),   # 用户当前配置
            ("swiftshader", 1, 2000, 0),
        ]

    print(f"参数矩阵：views={args.views}，共 {len(combos)} 组（每组独立子进程）")
    print(f"{'#':>3} {'backend':>12}{'limit':>6}{'height':>8}{'idle':>6}{'主进程Δ':>10}{'子进程Δ':>10}{'总Δ(MB)':>10}")
    print("-" * 76)

    results = []
    base_total = None
    for idx, (backend, limit, height, idle) in enumerate(combos, 1):
        r = _run_child(child, args.views, limit, height, idle, args.settle, args.timeout, backend)
        if r is None:
            print(f"{idx:>3} {backend:>12}{limit:>6}{height:>8}{idle:>6}{'—':>10}{'—':>10}{'失败/超时':>10}")
            continue
        results.append(r)
        if base_total is None:
            base_total = r["total_delta_mb"]
        save = base_total - r["total_delta_mb"]
        print(
            f"{idx:>3} {backend:>12}{limit:>6}{height:>8}{idle:>6}"
            f"{r['main_delta_mb']:>10.1f}{r['child_delta_mb']:>10.1f}{r['total_delta_mb']:>10.1f}"
            f"   省 {save:+.1f}MB"
        )

    if results:
        best = min(results, key=lambda x: x["total_delta_mb"])
        print("-" * 76)
        print(
            f"最优组合：backend={best['backend']} limit={best['limit']} "
            f"height={best['height']} idle={best['idle']} "
            f"→ 总增量 {best['total_delta_mb']} MB（基准 {base_total} MB，省 {base_total - best['total_delta_mb']:.1f} MB）"
        )

    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"已落盘: {args.json}")

    try:
        os.remove(child)
    except OSError:
        pass


if __name__ == "__main__":
    main()
