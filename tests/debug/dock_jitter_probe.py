# -*- coding: utf-8 -*-
"""坞态折叠栏抖动复现探针：简洁模式 + 大量正文流式 + 沉底工具与思考区。

背景
----
用户反馈：简洁模式下大量正文流式输出时，消息卡片下方的「工具与思考」
折叠栏（坞态沉底 #tool-section）频繁抖动。

判据（JS rAF 每帧采样）
------------------------
* gapBottom = body.clientHeight - tool-section 底边：折叠栏底边距视口底
  的距离。坞态贴底理想恒为 0；来回波动（尤其方向翻转）= 折叠栏抖动。
* drift = scrollHeight - scrollTop - clientHeight：body 贴底偏差，
  >0 表示内容超出视口（折叠栏可能被裁）。
* viewerH（Qt 侧同拍采样）与 bodySH 的差：高度应用滞后。

运行：
  python tests/debug/dock_jitter_probe.py [tag]
"""

import os
import sys

os.environ.setdefault("DRIFOX_DATA_DIR", os.path.join(os.path.dirname(__file__), "_probe_data"))

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

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

# rAF 每帧采样折叠栏几何（上限 6000 帧 ≈ 100s）
SAMPLE_JS = r"""
(function() {
    if (window.__dj) return 'exists';
    window.__dj = { rows: [], n: 0 };
    function _cap() {
        var ts = document.getElementById('tool-section');
        var tc = document.getElementById('tool-content');
        var b = document.body;
        if (!ts || !b) return;
        if (window.__dj.n >= 6000) return;
        var r = ts.getBoundingClientRect();
        var gapBottom = Math.round(b.clientHeight - r.bottom);
        var drift = b.scrollHeight - b.scrollTop - b.clientHeight;
        var cp = document.getElementById('content-placeholder');
        var stray = cp ? cp.querySelectorAll('.think-block,.think-streaming,.think-compact,[data-tool-call-id]').length : -1;
        window.__dj.rows.push([Math.round(performance.now()), gapBottom, drift, b.scrollHeight, b.scrollTop, b.clientHeight,
            tc ? tc.scrollTop : -1, tc ? tc.children.length : -1, ts.offsetHeight, stray,
            Math.round(r.top), tc ? tc.scrollHeight : -1]);
        window.__dj.n++;
    }
    (function _loop() { _cap(); requestAnimationFrame(_loop); })();
    return 'ok';
})()
"""

FETCH_JS = "JSON.stringify(window.__dj ? window.__dj.rows : [])"

BODY = (
    "## 分段标题\n\n这是流式正文输出，用于观察简洁模式坞态下折叠栏的稳定性。"
    "中文长段落持续追加，观察底部工具与思考折叠栏是否频繁上下抖动。\n\n"
    "- 列表项一：包含说明文字\n- 列表项二：更多内容\n\n"
    "```python\nprint('code block sample')\n```\n\n"
)


class Host(QWidget):
    """最小聊天区：QScrollArea + 纵向容器（复刻 main_widget 骨架）。"""

    def __init__(self, follow=True):
        super().__init__()
        self.follow = follow
        self.resize(900, 700)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.sa = QScrollArea()
        self.sa.setWidgetResizable(True)
        outer.addWidget(self.sa)
        self.container = QWidget()
        self.lay = QVBoxLayout(self.container)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(12)
        self.sa.setWidget(self.container)
        self.samples = []

    def on_height_changed(self, _h):
        if not self.follow:
            return
        self.sa.verticalScrollBar().setValue(self.sa.verticalScrollBar().maximum())


def js_eval(card, script, wait_ms=400):
    """PyQt5 runJavaScript 回调风格求值，qWait 等待完成。"""
    holder = []
    card.viewer.page().runJavaScript(script, lambda v: holder.append(v))
    QTest.qWait(wait_ms)
    return holder[0] if holder else None


def fetch_rows(card):
    raw = js_eval(card, FETCH_JS, 800)
    rows = []
    try:
        if isinstance(raw, str):
            import json

            rows = json.loads(raw)
        elif isinstance(raw, list):
            rows = raw
    except Exception as e:  # noqa: BLE001
        print(f"  采样取回失败: {e}", flush=True)
    return rows


def analyze(tag, rows, viewer_samples):
    if not rows:
        print(f"[{tag}] 无采样数据", flush=True)
        return
    t0 = rows[0][0]
    gaps = [r[1] for r in rows]
    drifts = [r[2] for r in rows]
    n = len(rows)
    # gapBottom 方向翻转次数（连续非零差分符号变化）
    flips = 0
    prev_d = 0
    for i in range(1, n):
        d = rows[i][1] - rows[i - 1][1]
        if d and prev_d and d * prev_d < 0:
            flips += 1
        if d:
            prev_d = d
    nz_gap = sum(1 for g in gaps if g != 0)
    nz_drift = sum(1 for d in drifts if d != 0)
    sg = sorted(gaps)
    print(
        f"[{tag}] 采样 {n} 帧 / {(rows[-1][0] - t0)}ms | gapBottom min={min(gaps)} "
        f"p50={sg[n // 2]} p90={sg[int(n * 0.9)]} max={max(gaps)} | 非零 {nz_gap}/{n}={100.0 * nz_gap / n:.0f}% | 翻转 {flips}",
        flush=True,
    )
    sd = sorted(drifts)
    print(
        f"[{tag}] drift(贴底偏差) min={min(drifts)} p50={sd[n // 2]} p90={sd[int(n * 0.9)]} max={max(drifts)} | 非零 {nz_drift}/{n}={100.0 * nz_drift / n:.0f}%",
        flush=True,
    )
    # viewer 高度时序回缩检测（P008「生长中回缩」）
    if viewer_samples:
        shrinks = []
        for i in range(1, len(viewer_samples)):
            d = viewer_samples[i] - viewer_samples[i - 1]
            if d < 0:
                shrinks.append((i * 16, viewer_samples[i - 1], viewer_samples[i]))
        print(
            f"[{tag}] viewer.height 采样 {len(viewer_samples)}: final={viewer_samples[-1]} "
            f"回缩次数={len(shrinks)}",
            flush=True,
        )
        for ts, a, b in shrinks[:20]:
            print(f"    t={ts}ms {a} -> {b} ({b - a}px)", flush=True)
    # 变化点样例（前 40 个：折叠栏顶边 or scrollTop or 子块数变化）
    prev = None
    shown = 0
    for i, r in enumerate(rows):
        key = (r[6], r[7], r[8], r[10])
        if prev is None or key != prev:
            if shown < 40:
                print(
                    f"    t={r[0] - t0}ms tcScroll={r[6]} children={r[7]} tsH={r[8]} tsTop={r[10]} "
                    f"tcSH={r[11]} gapBottom={r[1]} drift={r[2]} SH={r[3]} bodyST={r[4]} clientH={r[5]} stray={r[9]}",
                    flush=True,
                )
                shown += 1
        prev = key


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "run"
    follow = "--follow" in sys.argv
    host = Host(follow=follow)
    host.show()
    QApplication.processEvents()

    card = MessageCard("assistant")
    card.ensure_rendered()
    host.lay.addWidget(card)
    card.heightChanged.connect(host.on_height_changed)
    QTest.qWait(1500)

    # ── 注入工具与思考块：让折叠栏有内容并沉底（坞态）──
    # ⚠️ 勿用 start_streaming_anim：其 _anim_timer 回调依赖 main_widget 上下文
    # （_parent.chat_scroll_area），探针环境无 _parent → 0xC0000005。
    # 直接开坞态复现「坞态 + 正文流式」场景。
    card.viewer._sync_streaming_dock(True)
    QTest.qWait(300)
    card.append_reasoning("先分析任务，规划执行步骤。")
    QTest.qWait(200)
    card.update_tool_streaming("t1", "read", {"path": "demo.py"})
    QTest.qWait(150)
    card.append_tool_result("read", {"path": "demo.py"}, "file content line1\nline2", success=True, tool_call_id="t1")
    QTest.qWait(300)
    card.update_tool_streaming("t2", "grep", {"pattern": "foo"})
    QTest.qWait(150)
    card.append_tool_result("grep", {"pattern": "foo"}, "match at line 10", success=True, tool_call_id="t2")
    QTest.qWait(500)

    # 确认坞态开启
    dock_state = js_eval(
        card,
        "JSON.stringify({dock: document.body.classList.contains('streaming-dock'),"
        " compact: window._toolCompactMode === true,"
        " tsH: (document.getElementById('tool-section')||{}).offsetHeight,"
        " disp: (document.getElementById('tool-section')||{}).style.display})",
    )
    print(f"[{tag}] 坞态状态: {dock_state}", flush=True)

    # 用户上滚模式：先滚离底部一段（阅读位置），非贴底态
    if not follow:
        sb = host.sa.verticalScrollBar()
        sb.setValue(max(0, sb.maximum() - 200))

    # 启动 rAF 采样
    card.viewer.page().runJavaScript(SAMPLE_JS)
    QTest.qWait(100)

    # ── 大量正文流式（交错形态：正文与思考行交替，折叠栏持续收块）──
    viewer_samples = []
    vs_timer = QTimer()
    vs_timer.setInterval(16)
    vs_timer.timeout.connect(lambda: viewer_samples.append(card.viewer.height() if card.viewer else -1))
    vs_timer.start()
    interleave = "--interleave" in sys.argv
    for _i in range(60):
        card.append_text(BODY)
        QTest.qWait(110)
        if interleave and _i % 6 == 5:
            card.append_reasoning(f"补充思考第 {_i} 段：检查上文是否需要修正。")
            QTest.qWait(80)
    QTest.qWait(800)

    # 流式结束（坞态归位 + 折叠）
    card.finish_streaming()
    QTest.qWait(1500)
    vs_timer.stop()

    rows = fetch_rows(card)
    analyze(tag, rows, viewer_samples)
    card.deleteLater()
    QTest.qWait(100)
    print("done", flush=True)


main()
