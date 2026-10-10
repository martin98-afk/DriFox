# -*- coding: utf-8 -*-
"""量化探针：简洁模式流式「文字成块蹦出」的根因验证。

假设
----
1. `_twStep` 的指数衰减（`n = buf.length * dt/CATCHUP_MS`）永远追不上 chunk
   到达速度 → 打字机缓冲长期积压，文字按帧慢速爬。
2. 每次 `updateTailHtml` / `updateContentAppend` 入口的 `_twReset()` 把未揭示
   缓冲**整块丢弃**，同时用「含全部文本」的 HTML 替换 DOM → 那一刻文字从
   「已揭示量」跳到「全部文本」。丢弃的字数 = 用户感知的「成块蹦出」幅度。

测量
----
- hook `_twReset` / `_twFlush`：记录调用时 `_tw.buf.length`（丢弃字数）
- 采样 `_tw.buf.length` 时序：验证积压是否长期非空（追不上）
- hook `updateTailHtml` / `updateContentAppend` / `updateContent`：调用计数

运行：python tests/debug/stream_typewriter_stutter_probe.py
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

# 简洁模式（工具/思考归拢 + 坞态）
try:
    from app.utils.config import Settings

    _s = Settings.get_instance()
    _s.ui_compact_tool_area.value = True
    print("[init] ui_compact_tool_area =", _s.ui_compact_tool_area.value, flush=True)
except Exception as e:  # noqa: BLE001
    print("[init] Settings 设置失败:", e, flush=True)

# 中文长文本：无 \\n\\n 空行（无硬边界），句号密集（软边界高频）
_SENT = (
    "这是一段用于测试流式输出流畅性的中文长文本，句子之间用中文句号分隔，"
    "段落内部没有空行，用于模拟大语言模型常见的输出形态。"
    "为了观察打字机揭示队列与差量渲染之间的交互，需要足够长的文本以触发多次软边界渲染。"
    "每个句子大约三十到四十个字，句号密度较高，能够频繁触发软边界的即时渲染路径。"
)
CHUNK = _SENT * 6  # ≈ 600 字
FEED_INTERVAL_MS = 120  # 每 120ms 喂一批（≈ 一个网络 chunk 的节奏）
FEED_CHARS = 12  # 60 字/秒（中速 LLM 输出）

PROBE_JS = r"""
(function() {
    if (window.__probe) return 'exists';
    var P = window.__probe = {
        resets: [], flushes: [], tails: [], appends: [], fulls: [],
        bufSamples: [], maxBuf: 0, samples: 0
    };
    var tw = window._tw;
    if (!tw) return 'no_tw';
    // hook _twReset / _twFlush：记录丢弃/排空的缓冲字数
    var _origReset = window._twReset;
    window._twReset = function() {
        var b = (window._tw && window._tw.buf) ? window._tw.buf.length : -1;
        P.resets.push(b);
        return _origReset.apply(this, arguments);
    };
    var _origFlush = window._twFlush;
    window._twFlush = function() {
        var b = (window._tw && window._tw.buf) ? window._tw.buf.length : -1;
        P.flushes.push(b);
        return _origFlush.apply(this, arguments);
    };
    // hook 渲染入口：记录调用时的缓冲积压
    function _wrap(name, arr) {
        var orig = window[name];
        if (typeof orig !== 'function') return;
        window[name] = function() {
            var b = (window._tw && window._tw.buf) ? window._tw.buf.length : -1;
            arr.push(b);
            return orig.apply(this, arguments);
        };
    }
    _wrap('updateTailHtml', P.tails);
    _wrap('updateContentAppend', P.appends);
    _wrap('updateContent', P.fulls);
    // 每 100ms 采样缓冲积压
    setInterval(function() {
        var b = (window._tw && window._tw.buf) ? window._tw.buf.length : -1;
        if (b > P.maxBuf) P.maxBuf = b;
        if (P.bufSamples.length < 400) P.bufSamples.push(b);
        P.samples++;
    }, 100);
    return 'ok';
})()
"""

FETCH_JS = "JSON.stringify(window.__probe)"


def main():
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
    print("[init] js_ready OK; compact =", viewer._tool_compact_mode, flush=True)

    r = viewer._run_js_sync(PROBE_JS, timeout_ms=3000)
    print("[init] probe:", r, flush=True)

    card.start_streaming_anim()
    QTest.qWait(150)

    sent = 0
    total = len(CHUNK)

    def feed():
        nonlocal sent
        if sent >= total:
            timer.stop()
            return
        piece = CHUNK[sent : sent + FEED_CHARS]
        sent += len(piece)
        card.append_text(piece)

    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(FEED_INTERVAL_MS)

    # 喂完 + 静置观察
    while sent < total:
        QTest.qWait(100)
    QTest.qWait(2500)

    raw = viewer._run_js_sync(FETCH_JS, timeout_ms=3000)
    print("\n===== 探针结果 =====", flush=True)
    try:
        import json

        p = json.loads(raw)
        print("采样点数:", p["samples"], flush=True)
        print("缓冲最大积压:", p["maxBuf"], "字符", flush=True)
        samples = p["bufSamples"]
        nonzero = [b for b in samples if b > 0]
        print(
            f"缓冲非空采样占比: {len(nonzero)}/{len(samples)} = "
            f"{(100.0 * len(nonzero) / max(1, len(samples))):.0f}%",
            flush=True,
        )
        if nonzero:
            print(
                f"非空积压: 均值 {sum(nonzero) / len(nonzero):.1f} 字符, 最大 {max(nonzero)}",
                flush=True,
            )
        print(f"\n_twReset 调用 {len(p['resets'])} 次，丢弃缓冲: {p['resets']}", flush=True)
        print(f"_twFlush 调用 {len(p['flushes'])} 次，排空缓冲: {p['flushes']}", flush=True)
        print(f"updateTailHtml 调用 {len(p['tails'])} 次，进入时积压: {p['tails']}", flush=True)
        print(f"updateContentAppend 调用 {len(p['appends'])} 次，进入时积压: {p['appends']}", flush=True)
        print(f"updateContent 调用 {len(p['fulls'])} 次，进入时积压: {p['fulls']}", flush=True)
        discarded = [b for b in p["resets"] if b > 0]
        if discarded:
            print(
                f"\n[结论] 打字机被打断 {len(discarded)} 次，累计丢弃 {sum(discarded)} 字符，"
                f"单次均值 {sum(discarded) / len(discarded):.1f} 字符（= 用户看到的成块跳变量）",
                flush=True,
            )
    except Exception as e:  # noqa: BLE001
        print("解析失败:", e, "raw:", raw[:2000], flush=True)

    print("\ndone", flush=True)


main()
