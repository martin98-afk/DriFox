# -*- coding: utf-8 -*-
"""二分定位：WebEngine 探针崩溃点。逐阶段打印。"""
import os
import sys

import faulthandler

faulthandler.enable()

os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data2"))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
print("step1: QApplication", flush=True)
app = QApplication.instance() or QApplication(sys.argv)
print("step2: qt ready", flush=True)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile()
print("step3: profile init", flush=True)

try:
    from app.utils.config import Settings

    Settings.get_instance()
    print("step3.5: settings ok", flush=True)
except Exception as e:  # noqa: BLE001
    print(f"step3.5 skip: {e}", flush=True)

from app.widgets.message_card import MessageCard  # noqa: E402

print("step4: import MessageCard", flush=True)
card = MessageCard("assistant")
print("step5: card built", flush=True)
from PyQt5.QtWidgets import QScrollArea, QVBoxLayout, QWidget  # noqa: E402

host = QWidget()
host.resize(900, 700)
outer = QVBoxLayout(host)
outer.setContentsMargins(0, 0, 0, 0)
sa = QScrollArea()
sa.setWidgetResizable(True)
outer.addWidget(sa)
container = QWidget()
lay = QVBoxLayout(container)
lay.setContentsMargins(0, 0, 0, 0)
lay.setSpacing(12)
sa.setWidget(container)
host.show()
# 顺序实验：先挂载（reparent 空 card），再触发懒渲染建 viewer
lay.addWidget(card)
QApplication.processEvents()
print("step10: host ok", flush=True)
card.ensure_rendered()
QTest.qWait(2000)
print("step10.5: lazy render after mount", flush=True)
card.viewer.page().runJavaScript("document.title")
QTest.qWait(300)
print("step10.6: js ok", flush=True)
# 绕开 start_streaming_anim（其 _anim_timer 回调依赖 main_widget 上下文，
# 探针环境无 _parent → 崩溃）。直接开坞态，复现「坞态 + 正文流式」场景。
card.viewer._sync_streaming_dock(True)
QTest.qWait(400)
print("step11: dock on via _sync_streaming_dock", flush=True)

card.append_reasoning("先分析任务，规划执行步骤。")
QTest.qWait(200)
print("step12: reasoning ok", flush=True)

card.update_tool_streaming("t1", "read", {"path": "demo.py"})
QTest.qWait(150)
card.append_tool_result("read", {"path": "demo.py"}, "file content line1\nline2", success=True, tool_call_id="t1")
QTest.qWait(300)
print("step13: tool1 ok", flush=True)

card.update_tool_streaming("t2", "grep", {"pattern": "foo"})
QTest.qWait(150)
card.append_tool_result("grep", {"pattern": "foo"}, "match at line 10", success=True, tool_call_id="t2")
QTest.qWait(500)
print("step14: tool2 ok", flush=True)

holder = []
card.viewer.page().runJavaScript(
    "JSON.stringify({dock: document.body.classList.contains('streaming-dock'),"
    " compact: window._toolCompactMode === true,"
    " tsH: (document.getElementById('tool-section')||{}).offsetHeight,"
    " disp: (document.getElementById('tool-section')||{}).style.display})",
    lambda v: holder.append(v),
)
QTest.qWait(400)
print(f"step15: dock state = {holder}", flush=True)

SAMPLE_JS = r"""
(function() {
    if (window.__dj) return 'exists';
    window.__dj = { rows: [], n: 0 };
    function _cap() {
        var ts = document.getElementById('tool-section');
        var b = document.body;
        if (!ts || !b) return;
        if (window.__dj.n >= 6000) return;
        var r = ts.getBoundingClientRect();
        var gapBottom = Math.round(b.clientHeight - r.bottom);
        var drift = b.scrollHeight - b.scrollTop - b.clientHeight;
        window.__dj.rows.push([Math.round(performance.now()), gapBottom, drift, b.scrollHeight, b.scrollTop, b.clientHeight]);
        window.__dj.n++;
    }
    (function _loop() { _cap(); requestAnimationFrame(_loop); })();
    return 'ok';
})()
"""
card.viewer.page().runJavaScript(SAMPLE_JS)
QTest.qWait(100)
print("step16: sampler on", flush=True)

BODY = (
    "这是流式正文输出，用于观察简洁模式坞态下折叠栏的稳定性。"
    "中文长段落持续追加，观察底部工具与思考折叠栏是否频繁上下抖动。"
)
for _i in range(60):
    card.append_text(BODY)
    QTest.qWait(110)
print("step17: streamed 60 chunks", flush=True)
QTest.qWait(800)
print("step18: settled", flush=True)

card.finish_streaming()
QTest.qWait(1500)
print("step19: finished", flush=True)

rows_holder = []
card.viewer.page().runJavaScript("JSON.stringify(window.__dj ? window.__dj.rows.slice(0, 999999) : [])", lambda v: rows_holder.append(v))
QTest.qWait(800)
import json as _json  # noqa: E402

rows = _json.loads(rows_holder[0]) if rows_holder and rows_holder[0] else []
print(f"step20: rows={len(rows)}", flush=True)
if rows:
    t0 = rows[0][0]
    gaps = [r[1] for r in rows]
    drifts = [r[2] for r in rows]
    n = len(rows)
    flips = 0
    prev_d = 0
    for i in range(1, n):
        d = rows[i][1] - rows[i - 1][1]
        if d and prev_d and d * prev_d < 0:
            flips += 1
        if d:
            prev_d = d
    nzg = sum(1 for g in gaps if g != 0)
    nzd = sum(1 for d in drifts if d != 0)
    sg = sorted(gaps)
    sd = sorted(drifts)
    print(f"[RESULT] {n} 帧 / {rows[-1][0] - t0}ms | gapBottom min={min(gaps)} p50={sg[n//2]} p90={sg[int(n*0.9)]} max={max(gaps)} 非零 {nzg}/{n}={100.0*nzg/n:.0f}% 翻转 {flips}", flush=True)
    print(f"[RESULT] drift min={min(drifts)} p50={sd[n//2]} p90={sd[int(n*0.9)]} max={max(drifts)} 非零 {nzd}/{n}={100.0*nzd/n:.0f}%", flush=True)
    prev = None
    shown = 0
    for r in rows:
        if prev is None or r[1] != prev:
            if shown < 40:
                print(f"    t={r[0]-t0}ms gapBottom={r[1]} drift={r[2]} SH={r[3]} scrollTop={r[4]} clientH={r[5]}", flush=True)
                shown += 1
        prev = r[1]
card.deleteLater()
print("done", flush=True)
