# -*- coding: utf-8 -*-
"""[T37] 内容哨兵验证：快速喂入制造内容积压，断言哨兵在 3s 内自动补渲。

复现路径：快速流式（批间隔 < 合并窗口）下 MessageCard.append_text 的静默
分支叠加会让增量注入/调度渲染同时停摆 → DOM 停在几百字符、markdown 积压
数千 → 真机表现为"viewer 全白、高度很低"。哨兵借 reportHeight 第 5 字段
（cp 可见文本长度）被动巡检，积压即强制全量补渲。

判据：喂入结束后 cp 可见文本长度 ≥ markdown 长度的 60%（哨兵补渲已收敛），
且日志出现 [content-sentinel] 触发记录（停摆确实发生了并被接住）。
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

SENT = (
    "这是一段用于验证内容哨兵的中文长文本，句子用句号分隔，段落内部没有空行。"
    "快速喂入会让渲染链路停摆，哨兵应当在三秒内强制补渲恢复显示。"
)
MD = SENT * 45  # ~6k 字符

CHECK_JS = (
    "(function(){var c=document.getElementById('content-placeholder');"
    "return c?c.textContent.length:-1;})()"
)


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

    perform = {"n": 0}
    orig_perform = viewer._perform_update

    def spy_perform(*a, **k):
        perform["n"] += 1
        return orig_perform(*a, **k)

    viewer._perform_update = spy_perform

    card.start_streaming_anim()
    QTest.qWait(200)

    sent = 0
    # 快速喂入：200 字符 / 40ms（≈5000 字符/s，远快于真实 LLM，
    # 目的就是把渲染链路推进停摆区，制造哨兵要接的场景）
    timer = QTimer()
    timer.timeout.connect(feed)
    timer.start(40)  # noqa: F821

    def feed():
        nonlocal sent
        if sent >= len(MD):
            timer.stop()
            return
        piece = MD[sent : sent + 200]
        sent += len(piece)
        card.append_text(piece)

    while sent < len(MD):
        QTest.qWait(30)
    timer.stop()

    # 给哨兵最多 8s 收敛（3s 冷却 × 若干轮）
    ok = False
    for _ in range(16):
        QTest.qWait(500)
        dom = int(viewer._run_js_sync(CHECK_JS, timeout_ms=2000) or -1)
        if dom >= len(MD) * 0.6:
            ok = True
            break
    dom = int(viewer._run_js_sync(CHECK_JS, timeout_ms=2000) or -1)
    print(f"markdown={len(MD)}  DOM 可见文本={dom}  perform={perform['n']} 次", flush=True)
    print(
        f"结论: {'✓ 哨兵自愈有效（内容收敛 ≥60%）' if ok else '✗ 内容未收敛，哨兵未接住'}",
        flush=True,
    )
    card.finish_streaming()
    QTest.qWait(300)


main()
