# -*- coding: utf-8 -*-
"""诊断探针：真实流式管线「内容丢失」复现与量化。

背景/机制（由 stream_append_gate_cover_repro.py 已证实的 H1）
------------------------------------------------------------
`updateContentAppend(newHtml, tailHtml)` 的 `newHtml` 只含**本轮新闭合段**
（Python 侧每轮推进 `_stable_md_len`，后续轮次不再包含早前段）。而闸门
`window._twGate` 是单槽：挂起中的 `_gateFn` 被下一轮覆盖，覆盖时 `_gateExtra`
清空。注释的理由「新任务含更多文本，旧任务语义被包含」仅对 `updateTailHtml`
成立，对追加语义的 `updateContentAppend` **不成立** → 被覆盖那轮的段格式化
HTML 永不落地；随后执行的替换又 `remove` 全部 `[data-incremental]` 节点
（含承载该段纯文本的节点）→ 该段从屏上消失且无人补回。

本探针测真实管线是否命中，以及丢失量级
--------------------------------------
- 真实 MessageCard + 真实 QWebEngineView（非 offscreen，走真实渲染）
- 每个 chunk 携带唯一 token（T0001 递增），逐段 \n\n 闭合，模拟高速流式
- 喂完后**先读一次 DOM**（流式态，未终渲染）→ 统计缺失 token
- 再调 finish_streaming() 终渲染 → 再读 → 看是否被终渲染修复

运行：python tests/debug/stream_content_loss_live_probe.py
"""

import io
import json
import os
import re
import sys

os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data"))

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
_app = QApplication.instance() or QApplication(sys.argv)

# 探针参数（可 env 覆盖，便于扫参数找触发窗口）
FEED_INTERVAL_MS = int(os.environ.get("PROBE_INTERVAL_MS", "30"))
CHUNK_CHARS = int(os.environ.get("PROBE_CHUNK_CHARS", "14"))
PARA_COUNT = int(os.environ.get("PROBE_PARAS", "40"))

# 每段是一个完整段落（以 \n\n 收尾 → 硬边界 → 触发 immediate 差量渲染），
# 段内 token 形如 T0007，用 ASCII 避免被 markdown 转义影响检测。
PARA_TMPL = "这是第{idx:03d}段流式测试内容，用于验证正文完整性与渲染链路的稳定性。标记[{tok}]。\n\n"


def _build_text():
    parts = []
    for i in range(PARA_COUNT):
        parts.append(PARA_TMPL.format(idx=i, tok=f"T{i:04d}"))
    return "".join(parts)


_READ_JS = r"""
(function () {
    var c = document.getElementById('content-placeholder');
    var tc = document.getElementById('tool-content');
    return JSON.stringify({
        cp: c ? (c.textContent || '') : '',
        tc: tc ? (tc.textContent || '') : '',
        twBuf: (window._tw && window._tw.buf) ? window._tw.buf.length : -1,
        gateFn: !!(window._tw && window._tw._gateFn),
        gateExtra: (window._tw && window._tw._gateExtra) ? window._tw._gateExtra.length : 0
    });
})()
"""

# 闸门诊断计数：每次挂起/覆盖/放行都记账
_PROBE_JS = r"""
(function () {
    if (window.__lossProbe) return 'exists';
    var P = window.__lossProbe = {
        held: 0, drains: 0, queued: 0, deepQueue: 0, appends: 0, tails: 0, resets: 0
    };
    function _qlen() {
        return (window._tw && window._tw._gateQueue) ? window._tw._gateQueue.length : 0;
    }
    var _origGate = window._twGate;
    window._twGate = function (fn) {
        var before = _qlen();
        var r = _origGate.apply(this, arguments);
        if (r) {
            P.held++;
            P.queued++;
            if (before > 0) P.deepQueue++;   // 队列深度 >1：曾会被单槽丢弃的情形
        }
        return r;
    };
    var _origDrain = window._twDrainGate;
    window._twDrainGate = function () {
        var n = _qlen();
        var r = _origDrain.apply(this, arguments);
        if (n > 1) P.drains++;   // 一次放行多个挂起任务
        return r;
    };
    var _origReset = window._twReset;
    window._twReset = function () {
        if (_qlen()) P.resets++;
        return _origReset.apply(this, arguments);
    };
    function _wrap(name, key) {
        var orig = window[name];
        if (typeof orig !== 'function') return;
        window[name] = function () { P[key]++; return orig.apply(this, arguments); };
    }
    _wrap('updateContentAppend', 'appends');
    _wrap('updateTailHtml', 'tails');
    return 'ok';
})()
"""


def _tokens_in(text):
    return set(re.findall(r"T\d{4}", text or ""))


def main():
    from app.core.infra.webengine_profile import init_shared_web_profile
    from app.widgets.message_card import MessageCard

    init_shared_web_profile()

    card = MessageCard("assistant")
    card.resize(900, 760)
    card.show()
    card.ensure_rendered()

    for _ in range(80):
        QTest.qWait(150)
        v = card.viewer
        if v is not None and getattr(v, "_is_js_ready", False):
            break
    viewer = card.viewer
    if viewer is None or not getattr(viewer, "_is_js_ready", False):
        print("[FAIL] viewer 未就绪", flush=True)
        os._exit(1)

    print(f"[init] probe = {viewer._run_js_sync(_PROBE_JS, timeout_ms=3000)}", flush=True)
    print(
        f"[init] interval={FEED_INTERVAL_MS}ms chunk={CHUNK_CHARS}字 paras={PARA_COUNT}",
        flush=True,
    )
    # 对照实验开关：--gate-off 时禁用闸门（_twGate 恒返回 false → 立即执行），
    # 其余路径完全不变。用于验证「丢失」是否由闸门单槽覆盖引起（因果对照）。
    if "--gate-off" in sys.argv or os.environ.get("PROBE_GATE_OFF") == "1":
        r = viewer._run_js_sync(
            "(function(){window._twGate=function(){return false;};return 'gate-off';})()",
            timeout_ms=3000,
        )
        print(f"[init] 对照组：{r}", flush=True)

    card.start_streaming_anim()
    QTest.qWait(150)

    text = _build_text()
    pos = 0
    total = len(text)

    def feed():
        nonlocal pos
        if pos >= total:
            timer.stop()
            return
        piece = text[pos : pos + CHUNK_CHARS]
        pos += len(piece)
        card.append_text(piece)

    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(FEED_INTERVAL_MS)

    while pos < total:
        QTest.qWait(50)
    timer.stop()
    QTest.qWait(400)  # 让闸门/打字机收敛（不终渲染）

    mid = json.loads(viewer._run_js_sync(_READ_JS, timeout_ms=5000) or "{}")
    expect = _tokens_in(text)
    mid_have = _tokens_in(mid.get("cp", ""))
    mid_missing = sorted(expect - mid_have)

    print("\n===== 流式态（未终渲染） =====", flush=True)
    print(f"DOM 文本长度: {len(mid.get('cp', ''))} / 期望 {len(text)}", flush=True)
    print(
        f"token: 期望 {len(expect)} 个，DOM 中 {len(mid_have)} 个，缺失 {len(mid_missing)} 个",
        flush=True,
    )
    if mid_missing:
        print(f"缺失明细: {mid_missing[:30]}", flush=True)
    print(
        f"闸门统计: 挂起 {mid.get('gateFn')} 次 | "
        f"覆盖 {json.loads(viewer._run_js_sync('JSON.stringify(window.__lossProbe)') or '{}').get('overwritten')} 次",
        flush=True,
    )
    probe_snapshot = json.loads(viewer._run_js_sync("JSON.stringify(window.__lossProbe)") or "{}")
    print(f"[counters] {probe_snapshot}", flush=True)

    # 终渲染
    try:
        card.finish_streaming()
    except Exception as e:  # noqa: BLE001
        print("[warn] finish_streaming 异常:", e, flush=True)
    QTest.qWait(2500)

    fin = json.loads(viewer._run_js_sync(_READ_JS, timeout_ms=5000) or "{}")
    fin_have = _tokens_in(fin.get("cp", ""))
    fin_missing = sorted(expect - fin_have)
    print("\n===== 终渲染后 =====", flush=True)
    print(f"DOM 文本长度: {len(fin.get('cp', ''))} / 期望 {len(text)}", flush=True)
    print(
        f"token: DOM 中 {len(fin_have)} 个，缺失 {len(fin_missing)} 个",
        flush=True,
    )
    if fin_missing:
        print(f"缺失明细: {fin_missing[:30]}", flush=True)

    print("\n[TMP] raw_md 长度:", len(card.viewer._markdown_text or ""), flush=True)
    print("[TMP] stable_md_len:", card.viewer._stable_md_len, flush=True)

    print("\ndone", flush=True)
    sys.stdout.flush()
    os._exit(0)


main()
