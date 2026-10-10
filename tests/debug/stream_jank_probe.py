# -*- coding: utf-8 -*-
"""主线程 jank 探针：量化流式期间 UI 线程的帧节奏与阻塞点。

为什么需要它
------------
"流畅"的直接度量不是文字台阶大小，而是**主线程能否稳定按帧推进**。
任何一环（markdown 转换 / setHtml / setFixedHeight 回环 / 滚动跟随 /
Chromium 光栅化）阻塞主线程，都会表现为相邻帧间隔的突刺（jank）。
本探针用 0ms QTimer 采样主线程回调间隔，并分阶段打点，定位阻塞归属。

指标
----
- frame p50 / p90 / p99 / max：主线程两次回调的实际间隔（ms）
- jank(>33ms) / big-jank(>100ms)：掉帧与卡顿次数
- 分阶段耗时：append_text（Python 侧全链路）/ viewer 高度 setter
- Chromium 侧：rAF 帧间隔（页面内采样，反映渲染进程是否跟得上）

运行：python tests/debug/stream_jank_probe.py [tag]
"""

import os
import sys

os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data"))

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

init_shared_web_profile()

try:
    from app.utils.config import Settings

    Settings.get_instance().ui_compact_tool_area.value = True
except Exception:  # noqa: BLE001
    pass

_SENT = (
    "这是一段用于测试流式输出流畅性的中文长文本，句子之间用中文句号分隔，"
    "段落内部没有空行，用于模拟大语言模型常见的输出形态。"
    "为了观察打字机揭示队列与差量渲染之间的交互，需要足够长的文本以触发多次软边界渲染。"
    "每个句子大约三十到四十个字，句号密度较高，能够频繁触发软边界的即时渲染路径。"
)
TEXT_A = _SENT * 8

# 喂入节奏：模拟真实 LLM（约 100 字符/秒 ≈ 每 120ms 12 字符）
FEED_INTERVAL_MS = 120
FEED_CHARS = 12

# 页面侧 rAF 帧间隔采样（独立于主线程 QTimer 采样）
RAF_PROBE_JS = r"""
(function() {
    if (window.__jp) return 'exists';
    var P = window.__jp = { frames: [], longtasks: [] };
    var last = performance.now();
    (function loop(t) {
        var now = (typeof t === 'number' && t > 0) ? t : performance.now();
        if (P.frames.length < 4000) P.frames.push(Math.round(now - last));
        last = now;
        requestAnimationFrame(loop);
    })(last);
    try {
        new PerformanceObserver(function(list) {
            for (var e of list.getEntries()) {
                if (P.longtasks.length < 500) P.longtasks.push(Math.round(e.duration));
            }
        }).observe({entryTypes: ['longtask']});
    } catch (e) {}
    return 'ok';
})()
"""


def _pct(vals, q):
    if not vals:
        return 0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


def _report(label, vals):
    if not vals:
        print(f"  {label}: 无数据", flush=True)
        return
    p50 = _pct(vals, 0.50)
    p90 = _pct(vals, 0.90)
    p99 = _pct(vals, 0.99)
    mx = max(vals)
    jank = sum(1 for v in vals if v > 33)
    big = sum(1 for v in vals if v > 100)
    print(
        f"  {label}: p50={p50} p90={p90} p99={p99} max={mx}  "
        f"jank(>33ms)={jank}/{len(vals)}={100.0*jank/len(vals):.1f}%  big(>100ms)={big}",
        flush=True,
    )


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "run"
    card = MessageCard("assistant")
    card.resize(900, 800)
    card.show()
    card.ensure_rendered()
    for _ in range(60):
        QTest.qWait(200)
        v = card.viewer
        if v is not None and getattr(v, "_is_js_ready", False):
            break
    viewer = card.viewer
    if viewer is None or not getattr(viewer, "_is_js_ready", False):
        print("[FAIL] viewer 未就绪", flush=True)
        return
    print(f"[{tag}] raf probe:", viewer._run_js_sync(RAF_PROBE_JS, timeout_ms=3000), flush=True)

    card.start_streaming_anim()
    QTest.qWait(200)

    frames = []  # 主线程 QTimer 回调间隔
    append_cost = []  # card.append_text 耗时
    height_cost = []  # viewer 高度设置耗时（若可插桩）

    import time

    _perf = time.perf_counter
    last_ts = _perf()

    def on_tick():
        nonlocal last_ts
        now = _perf()
        frames.append(int((now - last_ts) * 1000))
        last_ts = now

    tick = QTimer()
    tick.setTimerType(Qt.PreciseTimer)
    tick.timeout.connect(on_tick)
    tick.start(1)

    # 插桩 append_text 的耗时
    _orig_append = card.append_text

    def timed_append(text):
        t0 = _perf()
        _orig_append(text)
        append_cost.append(int((_perf() - t0) * 1000))

    # 插桩 viewer 的高度设置（WebEngine resize 的核心动作）
    _orig_setfixed = viewer.setFixedHeight

    def timed_setfixed(h):
        t0 = _perf()
        _orig_setfixed(h)
        height_cost.append(int((_perf() - t0) * 1000))

    viewer.setFixedHeight = timed_setfixed

    sent = 0
    total = len(TEXT_A)

    def feed():
        nonlocal sent
        if sent >= total:
            timer.stop()
            return
        piece = TEXT_A[sent : sent + FEED_CHARS]
        sent += len(piece)
        timed_append(piece)

    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(FEED_INTERVAL_MS)

    # 预热 1s 不计入（首帧编译/资源加载噪声）
    QTest.qWait(1000)
    frames.clear()
    append_cost.clear()
    height_cost.clear()
    viewer._run_js_sync("if(window.__jp){window.__jp.frames=[];window.__jp.longtasks=[];}", timeout_ms=2000)
    t_start = _perf()

    while sent < total:
        QTest.qWait(30)
    QTest.qWait(1500)
    elapsed_ms = int((_perf() - t_start) * 1000)
    tick.stop()
    viewer.setFixedHeight = _orig_setfixed

    print(f"\n===== 流式期间主线程 jank [{tag}] （喂入 {total} 字符 / 历时 {elapsed_ms}ms）=====", flush=True)
    _report("主线程帧间隔", frames)
    _report("append_text 耗时", append_cost)
    _report("setFixedHeight 耗时", height_cost)
    print(
        f"  append_text 累计 {sum(append_cost)}ms ({100.0*sum(append_cost)/max(1,elapsed_ms):.1f}% 占用), "
        f"setFixedHeight 累计 {sum(height_cost)}ms, 调用 {len(height_cost)} 次",
        flush=True,
    )

    raw = viewer._run_js_sync(
        "(function(){var P=window.__jp;return JSON.stringify({f:P.frames.slice(0,3000),lt:P.longtasks});})()",
        timeout_ms=3000,
    )
    try:
        import json

        p = json.loads(raw)
        _report("页面 rAF 帧间隔", [v for v in p["f"] if v > 0])
        lt = p["lt"]
        if lt:
            print(f"  页面 longtask(>50ms): {len(lt)} 次, max={max(lt)}ms, 累计={sum(lt)}ms", flush=True)
        else:
            print("  页面 longtask(>50ms): 0 次", flush=True)
    except Exception as e:  # noqa: BLE001
        print("  页面采样解析失败:", e, str(raw)[:200], flush=True)

    card.finish_streaming()
    QTest.qWait(500)
    print("\ndone", flush=True)


main()
