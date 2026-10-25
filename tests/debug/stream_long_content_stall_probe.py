# -*- coding: utf-8 -*-
"""复现探针：消息卡片内容超长（>MAX_HEIGHT=3000px 进入卡内滚动）后流式更新失效+文字闪烁。

假设
----
1. 超长内容下增量链路某层静默失效（md 涨、DOM 文本停滞）→ [T37] 哨兵周期性
   强制全量补渲（3s 冷却）→ updateContent 整页 innerHTML 替换 = 文字闪烁。
2. 或超长后每轮都走全量渲染（updateContent 高频调用）→ 闪烁 + 打字机重置。

测量
----
- JS hook updateContent / updateContentAppend / updateTailHtml：调用序列与时间戳
- 每 200ms 采样：body scrollHeight/clientHeight（内滚模式开启时刻）、
  #content-placeholder 文本长度（DOM 实际可见内容量）、_tw.buf 积压
- Python 侧：_perform_update 调用时序（md_len / stable / needs_full）+ _rdiag 计数

运行：python tests/debug/stream_long_content_stall_probe.py
"""

import os
import sys
import time

os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data"))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

init_shared_web_profile()

# 中文长文本：段落带空行（硬边界）+ 长句（软边界），模拟正常 LLM 长回复
_PARA = (
    "这是一段用于复现超长卡片流式问题的中文正文内容，模拟大语言模型的长回复形态。"
    "每个段落包含若干长句，段落之间用空行分隔，用于触发硬边界的差量渲染路径。"
    "当卡片总高度超过 3000px 后正文会进入卡内滚动模式，需要观察此模式下增量链路是否停摆。"
)
CHUNK = ("\n\n" + _PARA) * 160  # ≈ 2 万字符，必超 3000px 进入卡内滚动
FEED_INTERVAL_MS = 30
FEED_CHARS = 60  # 2000 字/秒

PROBE_INSTALL_JS = r"""
(function() {
    if (window.__probe) return 'exists';
    var P = window.__probe = { calls: [], t0: performance.now() };
    function _wrap(name) {
        var orig = window[name];
        if (typeof orig !== 'function') return;
        window[name] = function() {
            P.calls.push({ n: name, t: Math.round(performance.now() - P.t0) });
            if (P.calls.length > 2000) P.calls.shift();
            return orig.apply(this, arguments);
        };
    }
    _wrap('updateContent');
    _wrap('updateContentAppend');
    _wrap('updateTailHtml');
    return 'ok';
})()
"""

SAMPLE_JS = r"""
JSON.stringify({
  sh: document.body ? document.body.scrollHeight : -1,
  ch: document.body ? document.body.clientHeight : -1,
  st: document.body ? document.body.scrollTop : -1,
  cp: (document.getElementById('content-placeholder') || {textContent:''}).textContent.length,
  buf: (window._tw && window._tw.buf) ? window._tw.buf.length : -1,
  fulls: window.__probe ? window.__probe.calls.filter(function(c){return c.n==='updateContent';}).length : -1
})
"""


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
    print("[init] js_ready OK", flush=True)

    # Python 侧：启用渲染诊断计数 + hook 关键调用
    from collections import Counter

    viewer._rdiag = Counter()
    py_log = []

    orig_pf = viewer._perform_update

    def pf_wrap():
        t = round(time.monotonic() * 1000) % 100000
        orig_pf()
        py_log.append((t, "pf", len(viewer._markdown_text), viewer._stable_md_len,
                       bool(viewer._needs_full_render), viewer._render_inflight))

    viewer._perform_update = pf_wrap

    orig_seq = viewer._sequence_render

    def seq_wrap(md, compact):
        t = round(time.monotonic() * 1000) % 100000
        py_log.append((t, "seq_submit", len(md), 0, False, False))
        return orig_seq(md, compact)

    viewer._sequence_render = seq_wrap

    r = viewer._run_js_sync(PROBE_INSTALL_JS, timeout_ms=3000)
    print("[init] probe hook:", r, flush=True)

    card.start_streaming_anim()
    QTest.qWait(150)

    sent = 0
    total = len(CHUNK)
    samples = []

    def feed():
        nonlocal sent
        if sent >= total:
            timer.stop()
            return
        piece = CHUNK[sent : sent + FEED_CHARS]
        sent += len(piece)
        card.append_text(piece)

    def sample():
        md = len(viewer._markdown_text)
        try:
            raw = viewer._run_js_sync(SAMPLE_JS, timeout_ms=1000)
            import json

            d = json.loads(raw)
        except Exception as e:
            d = {"err": str(e)[:60]}
        samples.append((round(time.monotonic() * 1000) % 100000, md, d))

    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(FEED_INTERVAL_MS)

    sampler = QTimer()
    sampler.timeout.connect(sample)
    sampler.start(200)

    while sent < total:
        QTest.qWait(100)
    QTest.qWait(4000)  # 静置观察哨兵补渲
    sampler.stop()

    # ── 分析 ──
    print("\n===== 采样时序（每 1s 抽 1 行）=====", flush=True)
    step = max(1, len(samples) // 40)
    for i in range(0, len(samples), step):
        t, md, d = samples[i]
        if "err" in d:
            print(f"t={t} md={md} err={d['err']}", flush=True)
            continue
        inner = "INNER" if d["sh"] > d["ch"] else "     "
        print(
            f"t={t} md={md} dom={d['cp']} inner_scroll={inner} "
            f"sh={d['sh']} ch={d['ch']} buf={d['buf']} fulls={d['fulls']}",
            flush=True,
        )

    print("\n===== JS 渲染调用分布 =====", flush=True)
    raw = viewer._run_js_sync("JSON.stringify(window.__probe.calls)", timeout_ms=3000)
    import json

    calls = json.loads(raw)
    from collections import Counter as C2

    dist = C2(c["n"] for c in calls)
    print("总调用:", dict(dist), flush=True)
    # updateContent 时间点（闪烁候选）
    fulls = [c["t"] for c in calls if c["n"] == "updateContent"]
    if fulls:
        print(f"updateContent 时间点({len(fulls)}):", fulls[:80], flush=True)

    print("\n===== Python 渲染时序（前 60 条）=====", flush=True)
    for row in py_log[:60]:
        print(row, flush=True)

    print("\n===== _rdiag 计数 =====", flush=True)
    print(dict(viewer._rdiag), flush=True)

    # 最终一致性：md 长度 vs DOM 文本长度
    final = samples[-1][2] if samples else {}
    print(f"\n[最终] md={len(viewer._markdown_text)} dom_cp={final.get('cp', '?')}", flush=True)
    gap = len(viewer._markdown_text) - final.get("cp", 0)
    print(f"[结论] md 与 DOM 差距: {gap} 字符（>800 即哨兵会周期性全量补渲=闪烁源）", flush=True)

    print("\ndone", flush=True)


main()
