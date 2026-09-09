# -*- coding: utf-8 -*-
"""WebEngine 并发内存探针 —— 定位「多并发对话内存暴涨 / 独显比核显更凶」的根因。

用法（在项目根目录，用项目 .venv）：

    .venv/Scripts/python.exe tools/diag_webengine_mem_probe.py
    .venv/Scripts/python.exe tools/diag_webengine_mem_probe.py --views 24
    .venv/Scripts/python.exe tools/diag_webengine_mem_probe.py --views 24 --angle d3d11
    .venv/Scripts/python.exe tools/diag_webengine_mem_probe.py --views 24 --share-gl
    .venv/Scripts/python.exe tools/diag_webengine_mem_probe.py --views 24 --no-disable-gpu

探针做四件事：
  A. 打印 GPU / DPI 相关的环境快照（这是「独显 vs 核显」分岔的源头）
  B. 实测 WebGL renderer 字符串（真 GPU / SwiftShader / 无）以及 GPU 子进程是否存在
  C. 逐个创建 N 个 QWebEngineView（模拟 N 张消息卡），每创建一批采样一次
     主进程 RSS 与 QtWebEngine 子进程 RSS（按 --type= 分类）
  D. 输出「每 view 平均增量」，这是判断合成表面成本的直接指标

关键判据（跑完后对照）：
  - 若存在 `--type=gpu` 子进程 → --disable-gpu 未真正拦住 GPU 进程，主因之一
  - 若 WebGL renderer 含 "SwiftShader"/"Google" → 软件光栅，独显未参与
  - 若 devicePixelRatio > 1 → 合成表面按 dpr² 放大，这是独显机器（高分屏）暴涨的乘数
  - 若 --share-gl 后每 view 增量明显下降 → 缺 AA_ShareOpenGLContexts 是主因
  - 若 --angle d3d11 后每 view 增量暴涨 → Qt 侧 ANGLE 后端走硬件是主因

本脚本不修改任何业务代码，只做测量。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# ── 环境变量必须在任何 Qt 导入之前设置（对齐 main.py 的顺序）──
# 这里由命令行参数控制，便于 A/B 对比。


def _parse_args():
    p = argparse.ArgumentParser(description="DriFox WebEngine 并发内存探针")
    p.add_argument("--views", type=int, default=16, help="创建的 QWebEngineView 数量（默认 16）")
    p.add_argument("--width", type=int, default=700, help="单 view 逻辑宽度（默认 700，对齐卡片实际宽度）")
    p.add_argument("--height", type=int, default=2000, help="单 view 逻辑高度（默认 2000）")
    p.add_argument(
        "--angle",
        default="warp",
        choices=["warp", "d3d11", "none"],
        help="QT_ANGLE_PLATFORM：warp=软件光栅（main.py 默认） / d3d11=硬件 / none=不设置",
    )
    p.add_argument("--share-gl", action="store_true", help="开启 AA_ShareOpenGLContexts（验证 GL 上下文共享的影响）")
    p.add_argument("--no-disable-gpu", action="store_true", help="去掉 --disable-gpu，让 Chromium 自由使用 GPU")
    p.add_argument("--enable-webgl", action="store_true", help="模拟 DRIFOX_ENABLE_WEBGL=1（改用 --enable-unsafe-swiftshader）")
    p.add_argument("--settle", type=float, default=1.2, help="每批创建后的稳定等待秒数（默认 1.2）")
    return p.parse_args()


_ARGS = _parse_args()

# ── Qt 环境（必须早于 QApplication / QtWebEngineWidgets 导入）──
os.environ.setdefault("QT_OPENGL", "angle")
if _ARGS.angle != "none":
    os.environ["QT_ANGLE_PLATFORM"] = _ARGS.angle
for _k in ("QSG_RHI", "QSG_RHI_BACKEND"):
    os.environ.pop(_k, None)

_gpu_flags = " --disable-software-rasterizer"
if _ARGS.enable_webgl:
    _gpu_flags = " --enable-unsafe-swiftshader"
elif not _ARGS.no_disable_gpu:
    _gpu_flags = " --disable-gpu --disable-software-rasterizer"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    "--renderer-process-limit=6" + _gpu_flags + " --disable-dev-shm-usage"
    " --enable-low-end-device-mode --disable-extensions"
)

import psutil  # noqa: E402
from PyQt5.QtCore import Qt, QTimer, QUrl  # noqa: E402
from PyQt5.QtGui import QGuiApplication  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
QApplication.setAttribute(Qt.AA_UseOpenGLES)
if _ARGS.share_gl:
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: E402


_MB = 1024.0 * 1024.0
_proc = psutil.Process()


def rss_mb() -> float:
    try:
        return _proc.memory_info().rss / _MB
    except Exception:
        return 0.0


def child_breakdown():
    """返回 {进程类型: [count, rss_mb]}，类型取自 cmdline 的 --type=xxx。"""
    out: dict[str, list] = {}
    try:
        kids = _proc.children(recursive=True)
    except Exception:
        return out
    for k in kids:
        try:
            rss = k.memory_info().rss / _MB
            cmd = " ".join(k.cmdline() or [])
        except Exception:
            continue
        if "QtWebEngineProcess" not in cmd and "qwebengine" not in cmd.lower():
            continue
        typ = "browser/other"
        for marker in ("--type=gpu", "--type=renderer", "--type=utility", "--type=zygote", "--type=broker"):
            if marker in cmd:
                typ = marker.split("=", 1)[1]
                break
        bucket = out.setdefault(typ, [0, 0.0])
        bucket[0] += 1
        bucket[1] += rss
    return out


def print_env_snapshot():
    print("=" * 78)
    print("A. 环境快照（独显 vs 核显的分岔点）")
    print("=" * 78)
    app = QGuiApplication.instance()
    for k in ("QT_OPENGL", "QT_ANGLE_PLATFORM", "QTWEBENGINE_CHROMIUM_FLAGS", "DRIFOX_ENABLE_WEBGL"):
        print(f"  {k:<32} = {os.environ.get(k, '(未设置)')}")
    try:
        scr = app.primaryScreen()
        dpr = scr.devicePixelRatio()
        logic = scr.logicalDotsPerInch()
        phys = scr.physicalDotsPerInch()
        geo = scr.geometry()
        print(f"  {'devicePixelRatio':<32} = {dpr}   ← 合成表面按 dpr² 放大")
        print(f"  {'logicalDotsPerInch':<32} = {logic:.1f}")
        print(f"  {'physicalDotsPerInch':<32} = {phys:.1f} (approx)")
        print(f"  {'screen geometry':<32} = {geo.width()}x{geo.height()}")
        print(f"  {'AA_ShareOpenGLContexts':<32} = {_ARGS.share_gl}")
        surf = _ARGS.width * dpr * _ARGS.height * dpr * 4 / _MB
        print(f"  {'单 view 合成表面(估算)':<32} = {surf:.1f} MB  ({_ARGS.width}x{_ARGS.height} @dpr={dpr}, RGBA)")
    except Exception as exc:
        print(f"  [!] 屏幕信息读取失败: {exc}")


_TYPICAL_HTML = """
<html><body style="margin:0;padding:12px;font:14px/1.6 system-ui;background:#fff">
<h3>典型消息卡片内容</h3>
<pre style="background:#f6f8fa;padding:10px;border-radius:6px"><code>
def example(items):
    return [x for x in items if x]
</code></pre>
<p>{i} — 这是一段用于模拟真实卡片正文的填充文本，包含中英文混排、
代码块、列表与表格占位，用于让 Chromium 真实完成一次布局与合成。</p>
<ul><li>条目一</li><li>条目二</li><li>条目三</li></ul>
</body></html>
"""


def _pump(seconds: float):
    """推进事件循环，让渲染真正发生（Chromium 是异步合成的）。"""
    app = QApplication.instance()
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main():
    app = QApplication(sys.argv)
    print_env_snapshot()

    print()
    print("=" * 78)
    print("B. GPU 路径实测")
    print("=" * 78)

    probe = QWebEngineView()
    probe.resize(400, 300)
    probe.show()
    probe.setHtml("<html><body>probe</body></html>", QUrl("about:blank"))
    _pump(1.5)

    _gl_result = {"value": None}

    def _on_gl(v):
        _gl_result["value"] = v

    probe.page().runJavaScript(
        """
        (function(){
            try {
                var c = document.createElement('canvas');
                var gl = c.getContext('webgl') || c.getContext('experimental-webgl');
                if (!gl) return 'NO_WEBGL_CONTEXT';
                var dbg = gl.getExtension('WEBGL_debug_renderer_info');
                if (dbg) return gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
                return gl.getParameter(gl.RENDERER);
            } catch (e) { return 'ERR: ' + e.message; }
        })()
        """,
        _on_gl,
    )
    _pump(1.5)
    print(f"  WebGL RENDERER = {_gl_result['value']}")
    print("   判读：含 SwiftShader/llvmpipe/Software → 软件光栅；")
    print("         含 NVIDIA/AMD/Intel(R) HD Graphics 且非 SwiftShader → 真 GPU 硬件路径")

    kids_before = child_breakdown()
    print(f"  WebEngine 子进程（探针阶段）: {kids_before or '{}'}")
    if "gpu" in kids_before:
        print("  ⚠️  存在 --type=gpu 进程：--disable-gpu 并未阻止 GPU 进程启动")
    else:
        print("  ✅ 未发现 --type=gpu 进程")

    probe.setHtml("<html><body></body></html>", QUrl("about:blank"))
    probe.close()
    probe.deleteLater()
    _pump(1.0)

    print()
    print("=" * 78)
    print(f"C. 并发 {_ARGS.views} 个 QWebEngineView 的 RSS 增量")
    print("=" * 78)

    base_rss = rss_mb()
    base_kids = child_breakdown()
    base_kid_total = sum(v[1] for v in base_kids.values())
    print(f"  基线: 主进程 RSS = {base_rss:.1f} MB | 子进程合计 = {base_kid_total:.1f} MB {base_kids}")

    views = []
    step = max(1, _ARGS.views // 8)
    print()
    print(f"  {'views':>6} | {'主进程RSS':>10} | {'子进程合计':>10} | {'总增量':>10} | {'每view增量':>11}")
    print("  " + "-" * 62)

    for i in range(_ARGS.views):
        v = QWebEngineView()
        v.resize(_ARGS.width, _ARGS.height)
        v.setHtml(_TYPICAL_HTML.format(i=i), QUrl("about:blank"))
        views.append(v)
        if (i + 1) % step == 0 or (i + 1) == _ARGS.views:
            _pump(_ARGS.settle)
            cur = rss_mb()
            kb = child_breakdown()
            ksum = sum(x[1] for x in kb.values())
            total = (cur + ksum) - (base_rss + base_kid_total)
            per = total / (i + 1)
            print(f"  {i + 1:>6} | {cur:>9.1f}M | {ksum:>9.1f}M | {total:>9.1f}M | {per:>10.2f}M")

    print()
    print("  子进程明细（按类型）:")
    for typ, (cnt, rss) in sorted(child_breakdown().items()):
        print(f"    {typ:<12} count={cnt:<3} rss={rss:.1f} MB")

    print()
    print("=" * 78)
    print("D. 判读建议")
    print("=" * 78)
    print("  1) 每 view 增量 > 20MB  → 合成表面成本过高，优先降低 view 数量与 dpr 放大")
    print("  2) 用 --share-gl 重跑：增量明显下降 → 缺 AA_ShareOpenGLContexts 是主因")
    print("  3) 用 --angle d3d11 重跑：增量暴涨 → Qt 侧 ANGLE 硬件后端是主因")
    print("  4) 用 --no-disable-gpu 重跑：出现 --type=gpu 且增量暴涨 → GPU 进程是主因")
    print("  5) devicePixelRatio > 1 时，合成表面按 dpr² 放大（1.5→2.25x，2.0→4x）")

    # 清理
    for v in views:
        try:
            v.setHtml("<html><body></body></html>", QUrl("about:blank"))
            v.close()
            v.deleteLater()
        except Exception:
            pass
    _pump(1.0)
    QTimer.singleShot(200, app.quit)
    app.exec_()


if __name__ == "__main__":
    main()
