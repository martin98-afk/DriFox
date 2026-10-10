# -*- coding: utf-8 -*-
"""流式「起步段」高度跳变探针。

用户现象
--------
流式刚开始时卡片高度突然跳变：先变得非常大，然后缩回正常高度。

本探针按真实调用序列驱动一张 assistant 卡片
（start_streaming_anim → reasoning → 正文 → finish），逐拍采样：

* Python 侧：viewer.height() / card.height()
* JS 侧：document.body.scrollHeight、#content-placeholder / #tool-section 的
  offsetHeight、body 是否带 streaming-dock、可见文本长度

输出前 N 秒的时间序列（只打变化行 + 峰值摘要），用于定位「哪一拍先胀后缩」
以及胀的是哪个区域。

运行：
  QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu --no-sandbox --disable-dev-shm-usage" \
    python tests/debug/stream_start_height_probe.py [tag]
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

# ── 采样脚本（JS 侧 16ms 一拍）──
PROBE_JS = r"""
(function() {
    window.__hp = {s: [], t0: performance.now()};
    setInterval(function() {
        var P = window.__hp;
        if (!P || P.s.length > 3000) return;
        var cp = document.getElementById('content-placeholder');
        var ts = document.getElementById('tool-section');
        var tc = document.getElementById('tool-content');
        P.s.push([
            Math.round(performance.now() - P.t0),
            document.body.scrollHeight,
            cp ? cp.offsetHeight : -1,
            ts ? ts.offsetHeight : -1,
            tc ? tc.offsetHeight : -1,
            document.body.classList.contains('streaming-dock') ? 1 : 0,
            cp ? cp.textContent.length : -1
        ]);
    }, 16);
    return 'ok';
})()
"""

FETCH_JS = "JSON.stringify(window.__hp)"


def _fmt_rows(rows):
    print(f"{'t(ms)':>7} {'viewer':>7} {'card':>7} {'bodyH':>7} {'cp':>7} {'ts':>6} {'tc':>6} {'dock':>4} {'txt':>6}")
    prev = None
    for r in rows:
        key = r[1:]
        if prev is not None and key == prev:
            continue
        prev = key
        print(
            f"{r[0]:>7} {r[1]:>7} {r[2]:>7} {r[3]:>7} {r[4]:>7} {r[5]:>6} {r[6]:>6} {r[7]:>4} {r[8]:>6}"
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
    print(f"[{tag}] probe:", viewer._run_js_sync(PROBE_JS, timeout_ms=3000), flush=True)

    # ── 采样容器 ──
    samples = []  # [t, viewer_h, card_h]
    timer = QTimer()
    timer.setInterval(16)
    timer.timeout.connect(
        lambda: samples.append([len(samples) * 16, viewer.height(), card.height()])
    )

    print("\n===== 起步段（start → reasoning → 正文）=====", flush=True)
    timer.start()
    card.start_streaming_anim()
    QTest.qWait(120)
    # 思考流
    for _i in range(12):
        card.append_reasoning("正在分析问题，逐步梳理关键信息，确认需要调用的工具与参数。")
        QTest.qWait(100)
    # 正文流
    body = (
        "这是流式正文输出，用于观察卡片高度随内容增长的变化节奏。"
        "每一批追加一小段中文文本，中间包含句号以触发软边界渲染。"
    )
    for _i in range(14):
        card.append_text(body)
        QTest.qWait(110)
    QTest.qWait(600)
    card.finish_streaming()
    QTest.qWait(1200)
    timer.stop()

    raw = viewer._run_js_sync(FETCH_JS, timeout_ms=4000)
    import json

    try:
        js = json.loads(raw)["s"]
    except Exception as e:  # noqa: BLE001
        print("解析失败:", e, str(raw)[:200], flush=True)
        return

    # 对齐：Python 采样起点与 JS 起点略有偏差，按索引对齐（同为 16ms 节拍，前 60 拍内足够准）
    n = min(len(samples), len(js))
    rows = []
    for i in range(n):
        py = samples[i]
        j = js[i]
        rows.append([py[0], py[1], py[2], j[1], j[2], j[3], j[4], j[5], j[6]])
    _fmt_rows(rows)

    hs = [r[1] for r in rows]
    print(
        f"\nviewer 高度: min={min(hs)} max={max(hs)} final={hs[-1]}；"
        f"峰值出现在 t={rows[hs.index(max(hs))][0]}ms"
    )
    bh = [r[3] for r in rows]
    print(f"body.scrollHeight: min={min(bh)} max={max(bh)} final={bh[-1]}")


main()
