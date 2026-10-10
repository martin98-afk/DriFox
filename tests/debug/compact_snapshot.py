# -*- coding: utf-8 -*-
"""简洁模式视觉快照：把「流式期间 / 结束后」两态渲染成 PNG，用于直观看效果。

为什么需要它
------------
简洁模式（ui_compact_tool_area）的行为散落在 CSS 坞态、compact 渲染分支、
finish 归位折叠里，读代码很难判断"用户到底看到什么"。本探针构造一张
含「长正文 + 代码块 + 思考块」的流式卡片，在流式中期与结束后各截一张图，
并把关键几何量（正文限高 / 工具区高度 / 卡片总高）一并打印。

运行：python tests/debug/compact_snapshot.py [输出目录]
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

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "_probe_data")

MD = (
    "我先看一下这段逻辑。\n\n"
    "<think>正在分析这段代码的时间复杂度，可能需要重构。</think>\n\n"
    "下面是实现：\n\n"
    "```python\ndef fib(n: int) -> int:\n"
    "    a, b = 0, 1\n"
    "    for _ in range(n):\n"
    "        a, b = b, a + b\n"
    "    return a\n```\n\n"
    "这个函数用迭代替代递归，时间复杂度 O(n)、空间 O(1)。\n\n"
    "另外还有几点需要注意：**第一**，边界条件；**第二**，溢出问题；**第三**，可读性。\n\n"
    + ("这一段是补充说明，用来把正文撑到超过原坞态 600px 限高，验证长回复在"
       "流式期间不再被压进内滚窗口、结束后也不再出现一次性的展开跳变。\n\n") * 4
    + "总结起来就是这样，希望对你有帮助。"
)

GEO_JS = r"""
(function() {
    var cp = document.getElementById('content-placeholder');
    var ts = document.getElementById('tool-section');
    var tc = document.getElementById('tool-content');
    return JSON.stringify({
        dock: document.body.classList.contains('streaming-dock'),
        compact: window._toolCompactMode !== false,
        contentScrollH: cp ? cp.scrollHeight : -1,
        contentClientH: cp ? cp.clientHeight : -1,
        contentMaxH: cp ? getComputedStyle(cp).maxHeight : '',
        toolH: ts ? ts.offsetHeight : -1,
        toolContentH: tc ? tc.offsetHeight : -1,
        toolCollapsed: ts ? ts.getAttribute('data-collapsed') : null,
        bodyH: document.body.scrollHeight
    });
})()
"""


def snap(card, name, geo_log):
    r = card.viewer._run_js_sync(GEO_JS, timeout_ms=3000)
    geo_log.append(f"  [{name}] {r}")
    try:
        pm = card.grab()
        p = os.path.join(OUT, f"compact_{name}.png")
        os.makedirs(OUT, exist_ok=True)
        pm.save(p)
        geo_log.append(f"  [{name}] 快照 -> {p} ({pm.width()}x{pm.height()})")
    except Exception as e:  # noqa: BLE001
        geo_log.append(f"  [{name}] 快照失败 {e}")


def main():
    print(f"输出目录: {OUT}", flush=True)
    card = MessageCard("assistant")
    card.resize(820, 900)
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
    print("compact =", viewer._tool_compact_mode, flush=True)

    log = []
    card.start_streaming_anim()
    QTest.qWait(300)

    sent = 0

    def feed():
        nonlocal sent
        if sent >= len(MD):
            timer.stop()
            return
        piece = MD[sent : sent + 30]
        sent += len(piece)
        card.append_text(piece)

    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(80)

    # 流式中期（约 55%）截一张
    while sent < int(len(MD) * 0.55):
        QTest.qWait(40)
    QTest.qWait(700)
    snap(card, "streaming", log)

    while sent < len(MD):
        QTest.qWait(40)
    timer.stop()
    QTest.qWait(600)
    snap(card, "pre_finish", log)

    card.finish_streaming()
    QTest.qWait(2000)
    snap(card, "finished", log)

    print("\n".join(log), flush=True)
    print("\ndone", flush=True)


main()
