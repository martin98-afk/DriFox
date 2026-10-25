# -*- coding: utf-8 -*-
"""复现探针 2：思考+工具+长正文混合流式——工具完成后正文持续流式的渲染分支观测。

假设（基于静态分析）
--------------------
1. append_tool_result 把懒回调（_build_incremental_md）设回 viewer → 下一拍消费
   → _markdown_text 含闭合 <tool> 标签。
2. _restore_finished_ids 恒非空 → _has_active_tool_dom() 恒 True → 差量路径永久让位。
3. 降载路径（tail 行内化）要求 tail 无协议标签 → tail 永含 </tool> → 永久跳过。
4. ⇒ 工具完成后每个含新 chunk 的渲染节拍都走全量线程池渲染（setHtml 整页替换）
   = 周期性闪烁；长内容渲染慢 + seq 丢弃 → 流式观感失效。
5. 「长度过长」放大器：全量渲染耗时随内容增长，>chunk 间隔时 seq 丢弃率飙升，
   基线推进停滞，死循环锁定。

测量：updateContent 调用频率（闪烁源）、_rdiag 计数、DOM 停摆窗口。

运行：python tests/debug/stream_tool_tail_full_render_probe.py
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

_PARA = (
    "这是一段用于复现工具完成后长正文流式闪烁问题的中文正文内容，模拟大语言模型在工具调用后继续输出的长回复形态。"
    "每个段落包含若干长句，段落之间用空行分隔，用于触发硬边界的差量渲染路径。"
    "当卡片总高度超过 3000px 后正文会进入卡内滚动模式，需要观察此模式下全量渲染的触发频率。"
)
BODY = ("\n\n" + _PARA) * 160  # ≈ 2 万字符正文（工具完成后继续输出）
FEED_INTERVAL_MS = 30
FEED_CHARS = 60

PROBE_INSTALL_JS = r"""
(function() {
    if (window.__probe) return 'exists';
    var P = window.__probe = { calls: [], sizes: [], t0: performance.now() };
    function _wrap(name) {
        var orig = window[name];
        if (typeof orig !== 'function') return;
        window[name] = function() {
            P.calls.push({ n: name, t: Math.round(performance.now() - P.t0) });
            if (name === 'updateTailHtml' && arguments.length) P.sizes.push((arguments[0]||'').length);
            if (P.calls.length > 5000) P.calls.shift();
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
  cp: (document.getElementById('content-placeholder') || {textContent:''}).textContent.length,
  tools: document.querySelectorAll('[data-tool-call-id]').length
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

    from collections import Counter

    viewer._rdiag = Counter()
    py_log = []

    orig_pf = viewer._perform_update

    def pf_wrap():
        t = round(time.monotonic() * 1000) % 100000
        tail_probe = viewer._markdown_text[viewer._stable_md_len :]
        orig_pf()
        py_log.append((t, "pf", len(viewer._markdown_text), viewer._stable_md_len,
                       bool(viewer._needs_full_render),
                       bool(viewer._has_active_tool_dom()),
                       "<tool>" in tail_probe, "</tool>" in tail_probe, len(tail_probe)))

    viewer._perform_update = pf_wrap

    r = viewer._run_js_sync(PROBE_INSTALL_JS, timeout_ms=3000)
    print("[init] probe hook:", r, flush=True)

    card.start_streaming_anim()
    QTest.qWait(150)

    # ── 场景：思考 → 工具运行 → 工具完成 → 长正文 ──
    card.append_reasoning("用户要求分析这个问题，我需要先读取相关文件了解上下文，然后再给出分析结论。" * 3)
    QTest.qWait(400)
    card.update_tool_streaming("t1", "read_file", {"path": "src/main.py"})
    QTest.qWait(500)  # 工具运行 500ms（期间无正文 chunk）
    card.append_tool_result(
        tool_name="read_file",
        arguments={"path": "src/main.py"},
        result="import sys\nfrom app.main_widget import MainWindow\n\ndef main():\n    ...",
        success=True,
        tool_call_id="t1",
    )
    QTest.qWait(400)

    print("[scene] 工具完成后开始长正文流式", flush=True)
    sent = 0
    total = len(BODY)
    samples = []

    def feed():
        nonlocal sent
        if sent >= total:
            timer.stop()
            return
        piece = BODY[sent : sent + FEED_CHARS]
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
    QTest.qWait(4000)
    sampler.stop()

    # ── 分析 ──
    import json

    raw = viewer._run_js_sync("JSON.stringify(window.__probe.calls)", timeout_ms=3000)
    calls = json.loads(raw)
    from collections import Counter as C2

    dist = C2(c["n"] for c in calls)
    print("\n===== JS 渲染调用分布（工具完成后正文期） =====", flush=True)
    print("总调用:", dict(dist), flush=True)
    fulls = [c["t"] for c in calls if c["n"] == "updateContent"]
    if len(fulls) > 1:
        gaps = [fulls[i + 1] - fulls[i] for i in range(len(fulls) - 1)]
        print(f"updateContent {len(fulls)} 次，间隔中位 {sorted(gaps)[len(gaps)//2]}ms 最小 {min(gaps)}ms", flush=True)
        print("时间点:", fulls[:60], flush=True)
    # updateTailHtml 体量演化（闪烁强度指标：每次全量重建的 tail HTML 长度）
    sizes = json.loads(viewer._run_js_sync("JSON.stringify(window.__probe.sizes)", timeout_ms=3000))
    if sizes:
        print(f"\nupdateTailHtml 体量演化({len(sizes)}次):", sizes[:70], flush=True)

    print("\n===== 采样（每 1.5s 抽 1 行）=====", flush=True)
    step = max(1, len(samples) // 25)
    for i in range(0, len(samples), step):
        t, md, d = samples[i]
        if "err" in d:
            print(f"t={t} md={md} err={d['err']}", flush=True)
            continue
        print(f"t={t} md={md} dom={d['cp']} tools={d['tools']} 滞后={md - d['cp']}", flush=True)

    print("\n===== Python pf 时序（每 12 条抽 1；t,md,stable,needs_full,tool_dom,tail含<tool>,tail含</tool>,tail长） =====", flush=True)
    for row in py_log[::12]:
        print(row, flush=True)

    print("\n===== _rdiag =====", flush=True)
    print(dict(viewer._rdiag), flush=True)

    final = samples[-1][2] if samples else {}
    print(f"\n[最终] md={len(viewer._markdown_text)} dom_cp={final.get('cp', '?')}", flush=True)
    print("done", flush=True)


main()
