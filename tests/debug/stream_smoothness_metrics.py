# -*- coding: utf-8 -*-
"""流畅度度量探针：量化「文字跳变」与「卡片高度抖动」，用于修复前后对比。

指标
----
1. **文字跳变分布**：每 50ms 采样 #content-placeholder 可见文本长度，
   计算相邻采样增量。打字机平滑时增量为「小步均匀」；成块蹦字时出现
   大台阶（delta 突刺）。输出：p50 / p90 / max 台阶大小 + 大台阶(>4 字符)占比。
2. **卡片高度抖动**：采样 viewer.height()，统计相邻变化幅度与方向翻转次数
   （翻转 = 上下抖动）。
3. 打字机缓冲积压与 _twReset 丢弃量（保留第一版探针的指标）。

场景
----
A. 中文长段落（无 \\n\\n）：纯 tail 行内渲染路径（updateTailHtml）
B. 带 \\n\\n 分段的中文：差量追加路径（updateContentAppend）+ tail

运行：python tests/debug/stream_smoothness_metrics.py
      python tests/debug/stream_smoothness_metrics.py before|after   # 打标
"""

import json
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
# 场景 A：纯长段落（无 \n\n）
TEXT_A = _SENT * 6
# 场景 B：带段落分隔（\n\n 每段），触发差量追加路径
TEXT_B = "".join(f"{_SENT}\n\n" for _ in range(5))

FEED_INTERVAL_MS = 120
FEED_CHARS = 12

PROBE_JS = r"""
(function() {
    if (window.__probe) return 'exists';
    var P = window.__probe = {
        resets: [], bufSamples: [], calls: {tail: 0, append: 0, full: 0}
    };
    var tw = window._tw;
    if (!tw) return 'no_tw';
    var _origReset = window._twReset;
    window._twReset = function() {
        P.resets.push((window._tw && window._tw.buf) ? window._tw.buf.length : -1);
        return _origReset.apply(this, arguments);
    };
    ['updateTailHtml', 'updateContentAppend', 'updateContent'].forEach(function(name) {
        var orig = window[name];
        if (typeof orig !== 'function') return;
        var key = name === 'updateTailHtml' ? 'tail' : (name === 'updateContentAppend' ? 'append' : 'full');
        window[name] = function() {
            P.calls[key]++;
            return orig.apply(this, arguments);
        };
    });
    setInterval(function() {
        var cp = document.getElementById('content-placeholder');
        var b = (window._tw && window._tw.buf) ? window._tw.buf.length : -1;
        if (P.bufSamples.length < 2000) {
            P.bufSamples.push([Math.round(performance.now()), b, cp ? cp.textContent.length : -1]);
        }
    }, 50);
    return 'ok';
})()
"""

FETCH_JS = "JSON.stringify(window.__probe)"


def _stats(deltas, label):
    """输出增量分布统计。"""
    if not deltas:
        print(f"  {label}: 无数据", flush=True)
        return
    ds = sorted(deltas)
    n = len(ds)
    p50 = ds[n // 2]
    p90 = ds[int(n * 0.9)]
    big = [d for d in ds if d > 4]
    print(
        f"  {label}: 采样增量 p50={p50} p90={p90} max={ds[-1]}  "
        f"大台阶(>4字符) {len(big)}/{n} = {100.0 * len(big) / n:.0f}%",
        flush=True,
    )


def run_scenario(card, viewer, name, text):
    print(f"\n===== 场景 {name} =====", flush=True)
    viewer._run_js_sync("if(window.__probe){window.__probe.resets=[];window.__probe.bufSamples=[];}", timeout_ms=2000)
    viewer._run_js_sync("if(window.__probe){window.__probe.calls.tail=0;window.__probe.calls.append=0;window.__probe.calls.full=0;}", timeout_ms=2000)
    # 记录场景开始前的正文长度（用于内容完整性校验）
    _t0 = viewer._run_js_sync(
        "(function(){var cp=document.getElementById('content-placeholder');return cp?cp.textContent.length:-1;})()",
        timeout_ms=2000,
    )
    try:
        base_len = int(_t0)
    except Exception:  # noqa: BLE001
        base_len = -1

    sent = 0
    total = len(text)
    heights = []

    def feed():
        nonlocal sent
        if sent >= total:
            timer.stop()
            return
        piece = text[sent : sent + FEED_CHARS]
        sent += len(piece)
        card.append_text(piece)

    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(FEED_INTERVAL_MS)

    def sample_h():
        try:
            heights.append(viewer.height())
        except Exception:  # noqa: BLE001
            pass

    # [T34] 采样间隔 50 → 12ms：追踪 tick 已是 16ms 单帧量级，50ms 采样会把
    # 多个追踪步合并成一个读数，「步进变细」的收益被采样本身抹掉（实测
    # 16ms 拍 + 0.45 在 50ms 采样下反而显示幅度 p50 4→11px 的假恶化）。
    hs = QTimer()
    hs.setTimerType(Qt.PreciseTimer)
    hs.timeout.connect(sample_h)
    hs.start(12)

    while sent < total:
        QTest.qWait(8)
    QTest.qWait(2000)
    hs.stop()

    raw = viewer._run_js_sync(FETCH_JS, timeout_ms=3000)
    try:
        p = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print("解析失败:", e, str(raw)[:200], flush=True)
        return
    samples = p["bufSamples"]
    # 文字长度增量（可见节奏）
    text_deltas = []
    for i in range(1, len(samples)):
        d = samples[i][2] - samples[i - 1][2]
        if d > 0:
            text_deltas.append(d)
    _stats(text_deltas, "文字增长台阶")
    # 高度抖动
    hd = [heights[i] - heights[i - 1] for i in range(1, len(heights))]
    nz = [d for d in hd if d != 0]
    flips = sum(1 for i in range(1, len(nz)) if nz[i] * nz[i - 1] < 0)
    print(
        f"  卡片高度: 变化 {len(nz)} 次, 幅度 p50={sorted(abs(d) for d in nz)[len(nz) // 2] if nz else 0}, "
        f"max={max((abs(d) for d in nz), default=0)}, 方向翻转 {flips} 次",
        flush=True,
    )
    print(f"  渲染调用: tail={p['calls']['tail']} append={p['calls']['append']} full={p['calls']['full']}", flush=True)
    discs = [b for b in p["resets"] if b > 0]
    print(
        f"  _twReset {len(p['resets'])} 次, 其中丢弃缓冲 {len(discs)} 次, 累计丢弃 {sum(discs)} 字符, "
        f"单次 max={max(discs) if discs else 0}",
        flush=True,
    )
    # 内容完整性：场景结束后 DOM 可见文本应 >= 基线 + 本次发送字符数
    _t1 = viewer._run_js_sync(
        "(function(){var cp=document.getElementById('content-placeholder');return cp?cp.textContent.length:-1;})()",
        timeout_ms=2000,
    )
    try:
        end_len = int(_t1)
        # textContent 会把 markdown 语法字符也算入；发送的是原文，两者应量级一致
        # 完整性判据：增量不低于发送量的 90%（低估来自 markdown 语法符号被渲染消解）
        delta = end_len - base_len
        expect = len(text)
        ok = delta >= expect * 0.9
        print(
            f"  内容完整性: DOM 文本 +{delta} 字符 / 发送 {expect} 字符 → "
            f"{'✓ 无丢字' if ok else '✗ 疑似丢字'}",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  内容完整性: 校验异常 {e}", flush=True)


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
    r = viewer._run_js_sync(PROBE_JS, timeout_ms=3000)
    print(f"[{tag}] probe:", r, "compact =", viewer._tool_compact_mode, flush=True)

    card.start_streaming_anim()
    QTest.qWait(150)
    run_scenario(card, viewer, "A(纯长段落)", TEXT_A)
    run_scenario(card, viewer, "B(带\\n\\n分段)", TEXT_B)
    card.finish_streaming()
    QTest.qWait(800)
    print("\ndone", flush=True)


main()
