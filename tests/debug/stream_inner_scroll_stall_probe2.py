# -*- coding: utf-8 -*-
"""复现探针 2：真实长回复形态（think 块 + 工具块 + 超长正文 → 卡内滚动）流式失效+闪烁。

探针 1（stream_long_content_stall_probe.py，纯文本）结论：增量链路健康，
md/dom 同步（差距 314），哨兵未触发。真实 LLM 长回复几乎必带 <think> 块与
工具块，历史同型 bug（0a1b1788 / 78ca2487）都在工具/思考路径。

本探针按真实形态喂流：
  1. <think>...</think> 思考块（静默累积 → 闭合翻转全量）
  2. 长正文喂到进入卡内滚动（sh 稳定 = clientHeight 上限）
  3. 中途 append_tool_result（工具完成块，激活 _has_active_tool_dom 一拍）
  4. 继续长正文
观测：进入卡内滚动后 updateContent（整页替换）是否周期性出现（哨兵 3s 补渲
=闪烁源）；DOM 文本是否停滞（增量失效指纹）；_tw 是否被重建。
"""

import os
import sys
import time
import json

os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data"))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

init_shared_web_profile()

_PARA = (
    "这是一段用于复现超长卡片流式问题的中文正文内容，模拟大语言模型的长回复形态。"
    "每个段落包含若干长句，段落之间用空行分隔，用于触发硬边界的差量渲染路径。"
    "当卡片总高度超过 3000px 后正文会进入卡内滚动模式，需要观察此模式下增量链路是否停摆。"
)
BODY_CHUNK = ("\n\n" + _PARA) * 160
FEED_INTERVAL_MS = 30
FEED_CHARS = 60

PROBE_INSTALL_JS = r"""
(function() {
    if (window.__probe) return 'exists';
    var P = window.__probe = { calls: [], t0: performance.now() };
    function _wrap(name) {
        var orig = window[name];
        if (typeof orig !== 'function') return;
        window[name] = function() {
            P.calls.push({ n: name, t: Math.round(performance.now() - P.t0) });
            if (P.calls.length > 3000) P.calls.shift();
            return orig.apply(this, arguments);
        };
    }
    _wrap('updateContent');
    _wrap('updateContentAppend');
    _wrap('updateTailHtml');
    _wrap('updateDocument');
    return 'ok';
})()
"""

SAMPLE_JS = r"""
JSON.stringify({
  sh: document.body ? document.body.scrollHeight : -1,
  ch: document.body ? document.body.clientHeight : -1,
  cp: (document.getElementById('content-placeholder') || {textContent:''}).textContent.length,
  tc: (document.getElementById('tool-content') || {textContent:''}).textContent.length,
  buf: (window._tw && window._tw.buf) ? window._tw.buf.length : -1,
  twAlive: !!window._tw,
  fulls: window.__probe ? window.__probe.calls.filter(function(c){return c.n==='updateContent';}).length : -1,
  apps: window.__probe ? window.__probe.calls.filter(function(c){return c.n==='updateContentAppend';}).length : -1,
  tails: window.__probe ? window.__probe.calls.filter(function(c){return c.n==='updateTailHtml';}).length : -1
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

    r = viewer._run_js_sync(PROBE_INSTALL_JS, timeout_ms=3000)
    print("[init] probe hook:", r, flush=True)

    card.start_streaming_anim()
    QTest.qWait(150)

    # ── 阶段 1：think 块（静默累积 → 闭合）──
    print("\n[phase1] think 块", flush=True)
    card.append_text("<think>\n")
    for i in range(10):
        card.append_text(f"这是第 {i} 段思考内容，模拟模型在深度思考状态下的推理过程文本。" * 3)
        QTest.qWait(40)
    card.append_text("\n</think>\n\n")
    QTest.qWait(500)

    # ── 阶段 2：长正文直到进入卡内滚动 ──
    print("[phase2] 长正文 → 卡内滚动", flush=True)
    sent = 0
    total = len(BODY_CHUNK)
    inner_entered = False

    def feed():
        nonlocal sent
        if sent >= total:
            timer.stop()
            return
        piece = BODY_CHUNK[sent : sent + FEED_CHARS]
        sent += len(piece)
        card.append_text(piece)

    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(FEED_INTERVAL_MS)

    samples = []
    tool_done = False

    while sent < total:
        QTest.qWait(100)
        try:
            raw = viewer._run_js_sync(SAMPLE_JS, timeout_ms=1000)
            d = json.loads(raw)
        except Exception as e:
            d = {"err": str(e)[:60]}
        samples.append((round(time.monotonic() * 1000) % 100000, sent, d))
        # 进入卡内滚动（sh 稳定且 ≈ ch）后插入一次工具块
        if not inner_entered and "sh" in d and d["ch"] > 0 and d["sh"] <= d["ch"] + 50 and sent > 3000:
            inner_entered = True
            print(f"\n[phase3] 卡内滚动已进入 (sh={d['sh']} ch={d['ch']})，插入工具块", flush=True)
            card.append_tool_result(
                tool_name="read_file",
                arguments={"path": "src/main.py"},
                result="import sys\n\n" + ("line of content\n" * 40),
                success=True,
                tool_call_id="call_probe_1",
            )
        if inner_entered and not tool_done and sent > total * 0.55:
            tool_done = True
            print("[phase3] 第二个工具块（流式中段）", flush=True)
            card.append_tool_result(
                tool_name="grep",
                arguments={"pattern": "def main"},
                result="main.py:12:def main():",
                success=True,
                tool_call_id="call_probe_2",
            )

    print(f"[phase4] 喂完，静置 6s 观察哨兵周期", flush=True)
    t_end = time.monotonic() + 6.0
    while time.monotonic() < t_end:
        QTest.qWait(200)
        try:
            raw = viewer._run_js_sync(SAMPLE_JS, timeout_ms=1000)
            d = json.loads(raw)
        except Exception as e:
            d = {"err": str(e)[:60]}
        samples.append((round(time.monotonic() * 1000) % 100000, sent, d))

    # ── 分析 ──
    print("\n===== 采样时序（每 1s 抽 1 行）=====", flush=True)
    step = max(1, len(samples) // 45)
    for i in range(0, len(samples), step):
        t, s, d = samples[i]
        if "err" in d:
            print(f"t={t} sent={s} err={d['err']}", flush=True)
            continue
        inner = "INNER" if d["sh"] <= d["ch"] + 50 and d["sh"] > 0 and s > 3000 else "     "
        print(
            f"t={t} sent={s} dom_cp={d['cp']} dom_tc={d['tc']} {inner} "
            f"sh={d['sh']} ch={d['ch']} buf={d['buf']} tw={d['twAlive']} "
            f"fulls={d['fulls']} apps={d['apps']} tails={d['tails']}",
            flush=True,
        )

    print("\n===== JS 渲染调用分布 =====", flush=True)
    raw = viewer._run_js_sync("JSON.stringify(window.__probe.calls)", timeout_ms=3000)
    calls = json.loads(raw)
    from collections import Counter

    dist = Counter(c["n"] for c in calls)
    print("总调用:", dict(dist), flush=True)
    fulls = [c["t"] for c in calls if c["n"] == "updateContent"]
    if fulls:
        print(f"updateContent 时间点({len(fulls)}):", fulls[:100], flush=True)
        # 全量间隔分析（周期性 → 哨兵指纹）
        gaps = [b - a for a, b in zip(fulls, fulls[1:])]
        if len(gaps) >= 3:
            print(f"全量间隔(ms): {gaps[:40]}", flush=True)

    md = len(viewer._markdown_text)
    final = samples[-1][2] if samples else {}
    print(f"\n[最终] md={md} dom_cp={final.get('cp', '?')} dom_tc={final.get('tc', '?')}", flush=True)
    print("done", flush=True)


main()
