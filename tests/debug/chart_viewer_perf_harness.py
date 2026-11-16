# -*- coding: utf-8 -*-
"""ChartViewerCard 放大查看卡顿量化 harness（真实 Qt 反馈回路，禁 offscreen）。

目的：量化「放大查看卡片」（chart_viewer_card.py）内 QWebEngineView(echarts) 的卡顿来源，
对三条假设给出可复跑数字：

  H1  QWebEngineView 是原生 HWND，挂进 QScrollArea(widgetResizable=True) 后每次布局/
      滚动触发 geometry sync + Chromium 合成，echarts 的 window resize→chart.resize
      被高频全量重绘。→ 对照 B vs A。
  H2  全屏 canvas 像素面积比消息卡内 400px 小图大 4~8 倍，echarts 默认 animation +
      自动注入的 dataZoom inside 每帧全量 raster 超 16ms。→ 对照 A vs C（面积比 ≈5.8x）。
  H3  _EXPORT_JS 的 _enablePanZoom 给 window 挂 mousemove/mouseup 全局监听，拖拽每帧
      transform。→ 动态检查页面内 panZoom 是否真的启用（源码路径：直角系折线走
      dataZoom 分支，_enablePanZoom 不应被调用）。

对照组（同一 300 点折线 option，build_chart_viewer_html 生产同路径生成，is_dark=True）：
  A  1000×700  QWebEngineView 直挂普通 QWidget 布局（margins 与生产 content_layout 一致）
  B  1000×700  QWebEngineView 挂 qfluentwidgets ScrollArea(widgetResizable=True) 内
               （复刻 SystemCardFrame.scroll_area + content_widget 结构：QVBoxLayout
               margins(4,2,4,2) spacing 4）
  C  400×300   直挂普通 QWidget（消息卡小图尺寸量级，H2 面积对照）

页面探针（loadFinished 后 runJavaScript 注入）：
  - 包装 chart.resize 计数（window resize handler 内 chart.resize() 为运行时属性查找，
    load 后包装仍能拦到全部调用）
  - ResizeObserver 观察 #chart 计数
  - requestAnimationFrame 采帧间隔（>16.7ms 长帧 / >33ms 重帧）
  - wheel 事件计数（capture）+ echarts datazoom 事件计数
  - _enablePanZoom 启用标志检查（container._panZoom）

程序化事件（真实布局链传播，不绕过 Qt 布局）：
  resize 阶段  顶层容器宽度 ±100px 交替 20 次（A/C 直挂 / B 经 scroll_area→
               widgetResizable→content→webview），每次 pump 60ms
  wheel 阶段   向 webview.viewport() sendEvent QWheelEvent(angleDelta ±120) 20 次
               （生产路径：dataZoom inside zoomOnMouseWheel=true → 缩放重绘）
  scroll 阶段  仅 B：scroll_area 视口缩到 500 高出滚动条后 scrollbar.setValue 步进
               20 次（webview geometry 不变，分离「纯滚动合成」与「geometry 变更」）

运行：python tests/debug/chart_viewer_perf_harness.py   （真窗会短暂弹 3 个窗口）
输出：stdout A/B/C 对照表 + H1/H2/H3 机械判定 + tests/debug/_probe_data/chart_viewer_perf_results.json
失败退化：真窗/WebEngine 反馈回路建不起来时记录失败原因原文并退出（不伪造数字）。
"""

import base64
import json
import math
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "windows"  # 真窗平台，禁 offscreen（offscreen 渲染不可信）
os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from PyQt5.QtCore import QEventLoop, QPoint, QPointF, Qt, QTimer  # noqa: E402
from PyQt5.QtGui import QWheelEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication, QFrame, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)  # WebEngine/qfluentwidgets 要求，须在创建前
app = QApplication.instance() or QApplication([])

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.utils.diff_viewer import _load_html_to_webview  # noqa: E402
from app.widgets.cards.settings.chart_viewer_card import build_chart_viewer_html  # noqa: E402

# ── 常量 ──────────────────────────────────────────────────────────────────────
RESULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_data", "chart_viewer_perf_results.json")
PAD_X, PAD_Y = 8, 4          # 布局 margins(4,2,4,2) 的宽/高预留，容器 = webview + PAD
N_RESIZE, N_WHEEL, N_SCROLL = 20, 20, 20
WHEEL_STEP_MS, RESIZE_STEP_MS, SCROLL_STEP_MS = 100, 60, 40
_NO_PHASE = getattr(Qt, "NoScrollPhase", None)

# 生产同款 300 点折线 option：不设 animation/不设 dataZoom（走生产默认 animation:true +
# 卡片 JS 自动注入 dataZoom inside+slider 的路径）
def make_option(n: int = 300) -> dict:
    data = [round(50 + 40 * math.sin(i / 12.0) + 10 * math.sin(i / 3.7), 2) for i in range(n)]
    return {
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": [str(i) for i in range(n)]},
        "yAxis": {"type": "value"},
        "series": [{"name": "demo", "type": "line", "showSymbol": False, "data": data}],
    }


PAYLOAD_B64 = base64.b64encode(json.dumps(make_option()).encode("utf-8")).decode("ascii")

# ── 页面探针 JS ──────────────────────────────────────────────────────────────
_PROBE_JS = """
(function () {
    if (window.__probeInstalled) { return 'already'; }
    window.__probeInstalled = true;
    var P = window.__probe = { resizeCalls: 0, roCount: 0, wheelEvt: 0, dzCount: 0, frames: [], panZoomOn: false };
    if (typeof chart !== 'undefined' && chart) {
        var orig = chart.resize;
        chart.resize = function () { P.resizeCalls++; return orig.apply(chart, arguments); };
        try { chart.on('datazoom', function () { P.dzCount++; }); } catch (e) {}
    }
    var el = document.getElementById('chart');
    if (window.ResizeObserver && el) { new ResizeObserver(function () { P.roCount++; }).observe(el); }
    window.addEventListener('wheel', function () { P.wheelEvt++; }, { capture: true, passive: true });
    P.panZoomOn = !!(el && el._panZoom);
    var last = 0;
    (function loop() {
        var now = performance.now();
        if (last) { P.frames.push(now - last); }
        last = now;
        requestAnimationFrame(loop);
    })();
    return 'ok';
})()
"""

_RESET_JS = (
    "window.__probe && (window.__probe.frames.length = 0, window.__probe.sealed = null,"
    " window.__probe.resizeCalls = 0, window.__probe.roCount = 0, window.__probe.wheelEvt = 0,"
    " window.__probe.dzCount = 0); 'reset'"
)

# 封口：把阶段期间累积的帧样本搬走（splice 清空），后续稳态尾巴帧不混入本阶段样本
_SEAL_JS = (
    "(function(){ var P = window.__probe; if (!P) return 0;"
    " P.sealed = P.frames.splice(0, P.frames.length); return P.sealed.length; })()"
)

_COLLECT_JS = """
(function () {
    var P = window.__probe || {};
    var el = document.getElementById('chart');
    return { resizeCalls: P.resizeCalls || 0, roCount: P.roCount || 0, wheelEvt: P.wheelEvt || 0,
             dzCount: P.dzCount || 0, panZoomOn: !!P.panZoomOn,
             chartW: el ? el.clientWidth : -1, chartH: el ? el.clientHeight : -1,
             innerW: window.innerWidth, innerH: window.innerHeight,
             frames: (P.sealed && P.sealed.length) ? P.sealed : (P.frames || []) };
})()
"""

_READY_JS = "(typeof chart !== 'undefined' && !!chart) && (typeof echarts !== 'undefined')"


# ── 基础设施 ─────────────────────────────────────────────────────────────────
def pump(ms: int) -> None:
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.005)


def js(view, code: str, timeout_ms: int = 8000):
    """runJavaScript 同步包装（QEventLoop + 超时兜底）"""
    box = {}
    loop = QEventLoop()

    def cb(v):
        if not box.get("done"):
            box["done"] = True
            box["v"] = v
            loop.quit()

    view.page().runJavaScript(code, cb)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    return box.get("v")


def wheel_target(view):
    """QWebEngineView 的真实输入接收者是内部 focusProxy（RenderWidgetHostViewQtDelegateWidget）
    （PyQt5 sip 包装无 viewport()）；无 focusProxy 时退回 view 本身。"""
    t = view.focusProxy()
    return t if t is not None else view


def make_wheel_event(view, dy: int):
    pos = QPointF(200, 200)
    glob = QPointF(view.mapToGlobal(QPoint(200, 200)))
    if _NO_PHASE is not None:
        return QWheelEvent(pos, glob, QPoint(0, 0), QPoint(0, dy), Qt.NoButton, Qt.NoModifier, _NO_PHASE, False)
    return QWheelEvent(pos, glob, QPoint(0, 0), QPoint(0, dy), Qt.NoButton, Qt.NoModifier)


class Group:
    """一组对照容器。host=顶层 QWidget；resize 风暴统一作用于顶层容器走真实布局链。"""

    def __init__(self, name: str, vw: int, vh: int, use_scroll: bool):
        self.name, self.vw, self.vh, self.use_scroll = name, vw, vh, use_scroll
        self.win_w, self.win_h = vw + PAD_X, vh + PAD_Y
        self.win = QWidget()
        self.win.setWindowTitle(f"chart-perf-{name}")
        outer = QVBoxLayout(self.win)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = None
        from PyQt5.QtWebEngineWidgets import QWebEngineView

        self.view = QWebEngineView()
        self.view.setMinimumSize(vw, vh)  # 常态满幅；B 滚动阶段缩视口时保住 webview 高度
        if use_scroll:
            from qfluentwidgets import ScrollArea

            sa = ScrollArea(self.win)
            sa.setWidgetResizable(True)
            sa.setFrameShape(QFrame.NoFrame)
            content = QWidget()
            cl = QVBoxLayout(content)
            cl.setContentsMargins(4, 2, 4, 2)  # 与 SystemCardFrame.content_layout 一致
            cl.setSpacing(4)
            cl.addWidget(self.view)
            sa.setWidget(content)
            self.scroll = sa
            outer.addWidget(sa)
        else:
            wrap = QWidget()
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(4, 2, 4, 2)  # 与生产 content margins 一致，隔离容器变量
            wl.setSpacing(4)
            wl.addWidget(self.view)
            outer.addWidget(wrap)


def load_html(view, html: str):
    """生产同路径加载（diff_viewer._load_html_to_webview：临时文件 + setUrl），返回 (load_ms, ok)"""
    tmp_files: list = []
    t0 = time.perf_counter()
    _load_html_to_webview(view, html, tmp_files)
    loop = QEventLoop()
    state = {"ok": None}

    def on_load(ok):
        state["ok"] = ok
        loop.quit()

    view.loadFinished.connect(on_load)
    QTimer.singleShot(20000, loop.quit)
    loop.exec()
    return round((time.perf_counter() - t0) * 1000, 1), state["ok"]


def frame_stats(frames):
    if not frames:
        return {"n": 0, "sum_ms": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0, "long16": 0, "long33": 0}
    s = sorted(frames)

    def pct(p):
        return s[min(len(s) - 1, int(len(s) * p))]

    return {
        "n": len(s),
        "sum_ms": round(sum(s), 1),
        "p50": round(pct(0.50), 1),
        "p95": round(pct(0.95), 1),
        "max": round(max(s), 1),
        "long16": sum(1 for x in s if x > 16.7),
        "long33": sum(1 for x in s if x > 33.0),
    }


def collect(g: Group, stage: str, wall_s: float) -> dict:
    js(g.view, _SEAL_JS)  # 先封口阶段帧样本
    pump(200)             # 等最后一次布局落定再采 DOM 尺寸（新产生的稳态帧不混入已封口样本）
    d = js(g.view, _COLLECT_JS) or {}
    return {
        "stage": stage,
        "wall_ms": round(wall_s * 1000, 1),
        "resizeCalls": d.get("resizeCalls", 0),
        "roCount": d.get("roCount", 0),
        "wheelEvt": d.get("wheelEvt", 0),
        "dzCount": d.get("dzCount", 0),
        "frames": frame_stats(d.get("frames") or []),
        "domViewport": [d.get("innerW"), d.get("innerH")],
        "chartWH": [d.get("chartW"), d.get("chartH")],
        "qtWH": [g.view.width(), g.view.height()],
    }


# ── 阶段 ─────────────────────────────────────────────────────────────────────
def stage_resize(g: Group) -> dict:
    g.win.resize(g.win_w, g.win_h)
    pump(300)
    js(g.view, _RESET_JS)
    t0 = time.perf_counter()
    for i in range(N_RESIZE):
        w = g.vw + (100 if i % 2 == 0 else -100)  # 宽度 ±100px 交替
        g.win.resize(w + PAD_X, g.win_h)
        pump(RESIZE_STEP_MS)
    return collect(g, "resize", time.perf_counter() - t0)


def stage_wheel(g: Group) -> dict:
    js(g.view, _RESET_JS)
    t0 = time.perf_counter()
    for i in range(N_WHEEL):
        QApplication.sendEvent(wheel_target(g.view), make_wheel_event(g.view, 120 if i % 2 == 0 else -120))
        pump(WHEEL_STEP_MS)
    return collect(g, "wheel", time.perf_counter() - t0)


def stage_scroll(g: Group) -> dict:
    """仅 B：缩视口出滚动条 → 步进滚动（webview geometry 不变）。缩视口发生在 reset
    之前，其引发的 resize 重排不计入本阶段，本阶段数字=纯滚动合成开销。"""
    sa = g.scroll
    sa.resize(g.win_w, 500 + PAD_Y)  # 视口 500 高 → 滚动条出现（内容由 webview minHeight 保住 700）
    pump(400)
    js(g.view, _RESET_JS)
    bar = sa.verticalScrollBar()
    t0 = time.perf_counter()
    for i in range(N_SCROLL):
        bar.setValue(bar.minimum() + (bar.maximum() - bar.minimum()) * i // max(N_SCROLL - 1, 1))
        pump(SCROLL_STEP_MS)
    r = collect(g, "scroll", time.perf_counter() - t0)
    sa.resize(g.win_w, g.win_h)  # 恢复满幅（放 collect 之后，不污染本阶段计数）
    pump(300)
    return r


# ── 判定 ─────────────────────────────────────────────────────────────────────
def _ratio_verdict(tag: str, a: dict, b: dict) -> str:
    fa, fb = a["frames"], b["frames"]
    la, lb = fa["long16"], fb["long16"]
    if lb >= max(la, 1) * 2 and (lb - la) >= 3:
        return f"证实：{tag} 长帧 {lb} vs {la}（{lb / max(la, 1):.1f}x），p95 {fb['p95']} vs {fa['p95']}ms，max {fb['max']} vs {fa['max']}ms"
    if la == 0 and lb == 0:
        if fb["p95"] > fa["p95"] * 1.3 and (fb["p95"] - fa["p95"]) >= 3:
            return f"部分证实（无长帧但 p95 放大）：{tag} p95 {fa['p95']}→{fb['p95']}ms，max {fa['max']}→{fb['max']}ms"
        return f"证伪：两者均零长帧，p95 {fa['p95']} vs {fb['p95']}ms、max {fa['max']} vs {fb['max']}ms 相当"
    return f"证伪/无显著差异：{tag} 长帧 {la} vs {lb}，p95 {fa['p95']} vs {fb['p95']}ms"


def verdicts(res: dict) -> dict:
    A, B, C = res["groups"]["A"], res["groups"]["B"], res["groups"]["C"]
    v = {}
    v["H1"] = _ratio_verdict("H1[A/B·resize]（B 若比 A 差 2 倍→证实 ScrollArea 放大）", A["stages"]["resize"], B["stages"]["resize"])
    a_mix = {
        "frames": {
            "long16": A["stages"]["resize"]["frames"]["long16"] + A["stages"]["wheel"]["frames"]["long16"],
            "p95": max(A["stages"]["resize"]["frames"]["p95"], A["stages"]["wheel"]["frames"]["p95"]),
            "max": max(A["stages"]["resize"]["frames"]["max"], A["stages"]["wheel"]["frames"]["max"]),
        }
    }
    c_mix = {
        "frames": {
            "long16": C["stages"]["resize"]["frames"]["long16"] + C["stages"]["wheel"]["frames"]["long16"],
            "p95": max(C["stages"]["resize"]["frames"]["p95"], C["stages"]["wheel"]["frames"]["p95"]),
            "max": max(C["stages"]["resize"]["frames"]["max"], C["stages"]["wheel"]["frames"]["max"]),
        }
    }
    v["H2"] = _ratio_verdict("H2[C/A·resize+wheel]（A 若比小图 C 差 2 倍→证实面积效应）", c_mix, a_mix)
    pan = [A["panZoomOn"], B["panZoomOn"], C["panZoomOn"]]
    if not any(pan):
        v["H3"] = "证伪：三组 panZoomOn 均为 false——300 点折线是直角系图表，走卡片 JS 的 dataZoom 分支，_enablePanZoom 未被调用，window mousemove/mouseup 全局监听不存在（源码路径吻合：仅非直角系才走 panZoom）"
    else:
        v["H3"] = f"意外：panZoom 被启用（{pan}），需补测拖拽 transform 帧开销"
    return v


# ── 输出 ─────────────────────────────────────────────────────────────────────
def print_table(res: dict) -> None:
    print("\n## A/B/C 对照表（帧间隔由页面 rAF 采样，墙钟=Python 侧阶段耗时）\n")
    print("| 组 | 阶段 | 墙钟ms | chart.resize次数 | RO触发 | wheel事件 | datazoom | 帧数 | 长16帧 | 长33帧 | p50 | p95 | max | DOM viewport | chart canvas |")
    print("|----|------|--------|------------------|--------|-----------|----------|------|--------|--------|-----|-----|-----|--------------|--------------|")
    for name in ("A", "B", "C"):
        g = res["groups"][name]
        for stage, d in g["stages"].items():
            if d is None:
                print(f"| {name} | {stage} | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")
                continue
            f = d["frames"]
            print(
                f"| {name} | {stage} | {d['wall_ms']} | {d['resizeCalls']} | {d['roCount']} | {d['wheelEvt']} | {d['dzCount']} "
                f"| {f['n']} | {f['long16']} | {f['long33']} | {f['p50']} | {f['p95']} | {f['max']} "
                f"| {d['domViewport'][0]}x{d['domViewport'][1]} | {d['chartWH'][0]}x{d['chartWH'][1]} |"
            )
        print(f"| {name} | meta | load_ms={g['load_ms']} ok={g['load_ok']} chart_ready={g['chart_ready']} panZoomOn={g['panZoomOn']} |")
    print()


def degrade(reason: str, extra: dict | None = None) -> None:
    print("\n[FAIL] 真窗 / WebEngine 反馈回路未能建立，退化为失败记录（不伪造数字）：")
    print("  ", reason)
    payload = {"failed": True, "reason": reason, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "extra": extra or {}}
    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  已落盘 {RESULT_PATH}")


# ── 主流程 ───────────────────────────────────────────────────────────────────
def core() -> None:
    vendor = os.path.join(PROJECT_ROOT, "app", "resources", "web", "vendor", "echarts.min.js")
    print(f"[1/6] 环境：python {sys.version.split()[0]} / QT_QPA_PLATFORM={os.environ['QT_QPA_PLATFORM']} / vendor echarts {'存在' if os.path.isfile(vendor) else '缺失(将降级CDN)'}")
    try:
        init_shared_web_profile()
    except Exception as e:  # noqa: BLE001
        print(f"      init_shared_web_profile 失败（继续，用默认 profile）：{type(e).__name__}: {e}")

    print("[2/6] 构建三组真窗（A 直挂 1000x700 / B ScrollArea 1000x700 / C 直挂 400x300）+ 生产同路径加载")
    html = build_chart_viewer_html("echarts", PAYLOAD_B64, is_dark=True)
    groups = {}
    for name, vw, vh, sc in (("A", 1000, 700, False), ("B", 1000, 700, True), ("C", 400, 300, False)):
        g = Group(name, vw, vh, sc)
        g.win.resize(g.win_w, g.win_h)
        g.win.show()
        pump(300)
        load_ms, ok = load_html(g.view, html)
        ready = bool(js(g.view, _READY_JS))
        if not ok or not ready:
            raise RuntimeError(f"组 {name} 页面未就绪：loadFinished ok={ok}，chart_ready={ready}（load 耗时 {load_ms}ms）——检查 vendor/CDN 与临时文件加载路径")
        g.load_ms, g.load_ok, g.chart_ready = load_ms, bool(ok), ready
        js(g.view, _PROBE_JS)
        pump(500)  # 排掉 echarts 初始 animation 尾巴再开测
        groups[name] = g
        print(f"      组 {name}: load {load_ms}ms ok={ok} chart_ready={ready} qt_webview={g.view.width()}x{g.view.height()}")
    gA, gB, gC = groups["A"], groups["B"], groups["C"]

    print("[3/6] 阶段测量（A: resize+wheel；B: resize+wheel+scroll；C: resize+wheel）")
    res = {
        "meta": {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.version.split()[0],
            "qt_platform": os.environ.get("QT_QPA_PLATFORM"),
            "vendor_echarts_local": os.path.isfile(vendor),
            "payload": "300点折线 option b64（生产默认 animation + 自动注入 dataZoom inside+slider）",
            "events": {"resize": N_RESIZE, "wheel": N_WHEEL, "scroll": N_SCROLL},
            "qt_webview_A": [gA.view.width(), gA.view.height()],
            "qt_webview_B": [gB.view.width(), gB.view.height()],
            "qt_webview_C": [gC.view.width(), gC.view.height()],
        },
        "groups": {},
    }
    for g, stages in ((gA, ("resize", "wheel")), (gB, ("resize", "wheel", "scroll")), (gC, ("resize", "wheel"))):
        out = {"load_ms": 0, "load_ok": True, "chart_ready": True}
        for st in stages:
            fn = {"resize": stage_resize, "wheel": stage_wheel, "scroll": stage_scroll}[st]
            out[st] = fn(g)
            print(f"      组 {g.name} {st}: 完成")
        pan = js(g.view, _COLLECT_JS) or {}
        out["panZoomOn"] = bool(pan.get("panZoomOn"))
        res["groups"][g.name] = {
            "stages": {k: out[k] for k in ("resize", "wheel", "scroll") if k in out},
            "panZoomOn": out["panZoomOn"],
            "load_ms": g.load_ms,
            "load_ok": g.load_ok,
            "chart_ready": g.chart_ready,
        }
        g.win.hide()
        pump(200)

    print("[4/6] 机械判定 H1/H2/H3")
    v = verdicts(res)
    res["verdicts"] = v

    print("[5/6] 输出对照表")
    print_table(res)
    for k in ("H1", "H2", "H3"):
        print(f"- {k}: {v[k]}")

    print(f"[6/6] 落盘 {RESULT_PATH}")
    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    for g in (gA, gB, gC):
        g.win.close()
    app.processEvents()
    print("完成")


def main() -> None:
    try:
        core()
    except Exception as e:  # noqa: BLE001
        degrade(f"{type(e).__name__}: {e}")
        os._exit(2)
    os._exit(0)  # WebEngine 子进程清理偶发挂起，debug 脚本直接强退保 shell 不卡


if __name__ == "__main__":
    main()
