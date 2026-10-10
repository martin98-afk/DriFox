# -*- coding: utf-8 -*-
"""白屏低高度复现探针：三层高度对比（DOM 真实高度 / JS 上报 / viewer 实际高度）。

背景
----
真机（长回复 + 多工具 + todo 面板，流式 5 分钟）出现：viewer 只剩 ~45px 高、
内容全白。45 ≈ 最小高度 40 → 疑似高度上报链路从流式开始就没成功过，
或全部被吞（追踪活跃吞上报后 tick 未校正 / 防抖丢失 / ResizeObserver 未触发）。

判据
----
每 300ms 同步采样三层：
  A. cp/body 的 scrollHeight（DOM 真实需要的高度）
  B. JS 侧 reportHeight 最后一次上报值（window.__lastReport 注入）
  C. viewer.height()（Qt 实际应用值）
正常三者应同向增长且量级一致。背离点即故障层。

场景
----
1. 纯长正文（12k 字符）
2. 长正文 + 未闭合代码块（正文开头为 think/工具移走后的空段，模拟真机形态）
运行：python tests/debug/stream_blank_collapse_probe.py
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

# 打点：JS 侧记录每次 reportHeight 的值与时刻 + 分层计数（push/揭示/异常）
INSTRUMENT_JS = r"""
(function() {
    if (window.__bp) return 'exists';
    window.__bp = { reports: [], dfxCalls: 0, dfxErr: 0, dfxErrMsg: '' };
    var _orig = window.reportHeight;
    if (typeof _orig !== 'function') return 'no_reportHeight';
    window.reportHeight = function() {
        var h = document.body ? document.body.scrollHeight : -1;
        if (window.__bp.reports.length < 4000) window.__bp.reports.push([Math.round(performance.now()), h]);
        return _orig.apply(this, arguments);
    };
    var _origD = window.reportHeightDebounced;
    if (typeof _origD === 'function') {
        window.reportHeightDebounced = function() {
            var h = document.body ? document.body.scrollHeight : -1;
            if (window.__bp.reports.length < 4000) window.__bp.reports.push([Math.round(performance.now()), h, 'd']);
            return _origD.apply(this, arguments);
        };
    }
    // 分层打点：_twPush（Python→JS 入口）与 _dfxAppendStreamText（揭示执行）
    var _origPush = window._twPush;
    if (typeof _origPush === 'function') {
        window._twPush = function(text) {
            window.__bp.pushed = (window.__bp.pushed || 0) + (text ? text.length : 0);
            return _origPush.apply(this, arguments);
        };
    }
    var _origDfx = window._dfxAppendStreamText;
    if (typeof _origDfx === 'function') {
        window._dfxAppendStreamText = function() {
            window.__bp.dfxCalls++;
            try {
                return _origDfx.apply(this, arguments);
            } catch (e) {
                window.__bp.dfxErr++;
                if (!window.__bp.dfxErrMsg) window.__bp.dfxErrMsg = String(e && e.message || e).slice(0, 160);
                throw e;
            }
        };
    }
    return 'ok';
})()
"""

SAMPLE_JS = r"""
(function() {
    var cp = document.getElementById('content-placeholder');
    var ts = document.getElementById('tool-section');
    var tw = window._tw || {};
    return JSON.stringify({
        bodySH: document.body ? document.body.scrollHeight : -1,
        cpSH: cp ? cp.scrollHeight : -1,
        cpText: cp ? cp.textContent.length : -1,
        toolH: ts ? ts.offsetHeight : -1,
        dock: document.body.classList.contains('streaming-dock'),
        reports: window.__bp ? window.__bp.reports.length : -1,
        pushed: (window.__bp && window.__bp.pushed) || 0,
        dfxCalls: (window.__bp && window.__bp.dfxCalls) || 0,
        dfxErr: (window.__bp && window.__bp.dfxErr) || 0,
        dfxErrMsg: (window.__bp && window.__bp.dfxErrMsg) || '',
        twBuf: (tw.buf || '').length,
        twGateQ: (tw._gateQueue || []).length,
        twRaf: tw.raf || 0,
        twEnabled: tw.enabled !== false
    });
})()
"""

from app.widgets.card_render_core import (  # noqa: E402
    _has_unclosed_chart_fence,
    _has_unclosed_registered_tag,
)
from app.widgets.card_viewers import _has_unclosed_think  # noqa: E402

_SENT = (
    "这是一段用于复现白屏问题的中文长文本，句子之间用中文句号分隔，段落内部没有空行。"
    "为了把正文撑到远超视口的高度，这里重复多段相同结构的说明文字，"
    "每段约四十个字符，持续追加直到总量达到一万两千字。"
)
CODE_BLOCK = (
    "```python\n"
    + "\n".join(f"def func_{i}(x):\n    return x * {i}  # line {i}" for i in range(60))
    + "\n"
)


def run_case(name: str, md: str):
    print(f"\n===== {name} =====", flush=True)
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
        print("  FAIL viewer 未就绪", flush=True)
        return
    print("  instrument:", viewer._run_js_sync(INSTRUMENT_JS, timeout_ms=3000), flush=True)

    # Python 侧上报插桩：记录每次到达 _update_height 的 h
    py_reports = []
    orig_update = card._update_height

    def spy_update(h):
        py_reports.append(h)
        return orig_update(h)

    card._update_height = spy_update

    # 渲染链路插桩：append_chunk / _append_text_incremental / _perform_update
    py_stats = {"chunk": 0, "incr": 0, "incr_chars": 0, "perform": 0, "visible_false": 0, "js_ready_false": 0}
    vw = card.viewer
    orig_chunk = vw.append_chunk
    orig_incr = vw._append_text_incremental
    orig_perform = vw._perform_update

    def spy_chunk(t):
        py_stats["chunk"] += 1
        return orig_chunk(t)

    def spy_incr(text):
        if not vw._is_js_ready:
            py_stats["js_ready_false"] += 1
            return
        if not vw.isVisible():
            py_stats["visible_false"] += 1
            return
        py_stats["incr"] += 1
        py_stats["incr_chars"] += len(text or "")
        return orig_incr(text)

    def spy_perform(*a, **k):
        py_stats["perform"] += 1
        return orig_perform(*a, **k)

    vw.append_chunk = spy_chunk
    vw._append_text_incremental = spy_incr
    vw._perform_update = spy_perform

    card.start_streaming_anim()
    QTest.qWait(300)

    samples = []
    diag_lines = []
    sent = 0

    def feed():
        nonlocal sent
        if sent >= len(md):
            timer.stop()
            return
        piece = md[sent : sent + 120]
        sent += len(piece)
        card.append_text(piece)
        # 谓词级诊断：每 10 批打印一次 append_text 各静默分支的判定值
        if py_stats["chunk"] == 0 and (sent // 120) % 10 == 0:
            lb = card._content_data[-1] if card._content_data else {}
            lt = lb.get("text", "") if isinstance(lb, dict) else ""
            diag = (
                f"    feed@{sent // 120}: streaming={card._streaming} lazy={card._lazy_rendered} "
                f"viewer={'Y' if card.viewer else 'N'} jsReady={vw._is_js_ready} vis={vw.isVisible()} "
                f"thinkU={_has_unclosed_think(lt)} tagU={_has_unclosed_registered_tag(lt)} "
                f"fenceU={_has_unclosed_chart_fence(lt)} lastTextLen={len(lt)}"
            )
            diag_lines.append(diag)

    def sample():
        try:
            raw = viewer._run_js_sync(SAMPLE_JS, timeout_ms=1500)
            import json

            d = json.loads(raw)
            d["viewerH"] = viewer.height()
            samples.append(d)
        except Exception as e:  # noqa: BLE001
            samples.append({"err": str(e)[:80], "viewerH": viewer.height()})

    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(60)
    hs = QTimer()
    hs.timeout.connect(sample)
    hs.start(400)

    while sent < len(md):
        QTest.qWait(30)
    timer.stop()
    QTest.qWait(1500)
    sample()
    hs.stop()

    # 输出关键采样点：前 5、后 5、以及 viewerH 与 bodySH 背离最大的点
    def fmt(d):
        if "err" in d:
            return f"viewerH={d['viewerH']} ERR={d['err']}"
        return (
            f"viewerH={d['viewerH']:5d} bodySH={d['bodySH']:5d} cpText={d['cpText']:5d} "
            f"pushed={d['pushed']:5d} dfx={d['dfxCalls']:3d}/err{d['dfxErr']:2d} "
            f"twBuf={d['twBuf']:4d} gateQ={d['twGateQ']} raf={d['twRaf']} en={int(d['twEnabled'])} "
            f"toolH={d['toolH']:3d} dock={int(d['dock'])} jsRep={d['reports']}"
        )

    print("  --- 前 3 个采样 ---", flush=True)
    for d in samples[:3]:
        print("   ", fmt(d), flush=True)
    print("  --- 后 3 个采样 ---", flush=True)
    for d in samples[-3:]:
        print("   ", fmt(d), flush=True)
    diver = [
        (abs(d.get("bodySH", 0) - d.get("viewerH", 0)), i, d)
        for i, d in enumerate(samples)
        if "err" not in d
    ]
    diver.sort(reverse=True)
    if diver:
        _, i, d = diver[0]
        print(f"  --- 最大背离点 @样本{i} ---", flush=True)
        print("   ", fmt(d), flush=True)
    print(
        f"  Python _update_height 调用 {len(py_reports)} 次, 最新值={py_reports[-1] if py_reports else None}, "
        f"max={max(py_reports) if py_reports else None}",
        flush=True,
    )
    print("\n".join(diag_lines[:8]), flush=True)
    print(
        f"  渲染链路: chunk={py_stats['chunk']} perform={py_stats['perform']} "
        f"incr={py_stats['incr']}(chars={py_stats['incr_chars']}) "
        f"js_ready_false={py_stats['js_ready_false']} visible_false={py_stats['visible_false']}",
        flush=True,
    )

    card.deleteLater()
    QTest.qWait(100)


SENT6 = _SENT * 40  # ~6k 字符：远超哨兵 800 阈值，验证积压触发补渲
CASES = {
    "场景1 纯长正文": SENT6,
    "场景2 think+长正文+代码块": "<think>先思考一下实现方案，需要拆分步骤。</think>\n\n" + SENT6[:4000] + "\n\n" + CODE_BLOCK + "\n\n" + SENT6[4000:],
}

main_ = None
for name, md in CASES.items():
    try:
        run_case(name, md)
    except Exception as e:  # noqa: BLE001
        print(f"  [{name}] 异常 {e}", flush=True)
print("\ndone", flush=True)
