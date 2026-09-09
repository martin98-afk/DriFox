# -*- coding: utf-8 -*-
"""复现/验证：思考与工具的「预览文字」在落地时逐字显现（预览打字机）。

背景
----
思考内容与工具预览是**静默累积 → 一次性全量渲染**落地的（``append_reasoning`` 的
既定设计）：流式期间 DOM 上只有"深度思考中..." spinner，文字要等正文 chunk 或
工具调用触发全量渲染才出现，因此常被感知成"要等工具执行完才输出"。

本脚本驱动真实 ``MessageCard`` 走一遍最小流式序列，高频采样
``[data-dfx-preview]`` 的文本，用于回答两个问题：
1. 预览文字是否逐字增长（而不是整段瞬间出现）？
2. 流式期间频繁的全量重渲染（150~500ms 一次）会不会让已显示文字回退重打？

运行：python tests/debug/preview_typewriter_repro.py
（退出时 Qt/WebEngine teardown 可能报 0xC0000409，不影响采样输出）
"""

import sys

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)

from app.core.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

init_shared_web_profile()

SAMPLE_JS = """
(function() {
    var els = document.querySelectorAll('[data-dfx-preview]');
    var out = [];
    for (var i = 0; i < els.length; i++) out.push(els[i].textContent.trim());
    return JSON.stringify(out);
})()
"""


def sample(card):
    try:
        return card.viewer._run_js_sync(SAMPLE_JS, timeout_ms=2000)
    except Exception as e:  # noqa: BLE001
        return f"<err {e}>"


def new_card():
    c = MessageCard("assistant")
    c.resize(900, 700)
    c.show()
    c.ensure_rendered()
    for _ in range(40):
        QTest.qWait(200)
        if getattr(c.viewer, "_is_js_ready", False):
            break
    print("js_ready:", getattr(c.viewer, "_is_js_ready", False), flush=True)
    c.start_streaming_anim()
    QTest.qWait(200)
    return c


def main():
    c = new_card()
    c.start_new_thinking_block()
    c.append_reasoning("先确认文件位置，再决定怎么改，不要急着动手。")
    QTest.qWait(300)
    # 工具调用触发思考块落地（预览行出现）
    c.update_tool_streaming("call_tw_1", "read_file", {"path": "app/main.py"})
    print("[1] 预览文字采样（每 40ms）：", flush=True)
    for i in range(16):
        print(f"   t={i * 40}ms {sample(c)}", flush=True)
        QTest.qWait(40)

    # 再触发一次全量渲染：已显示文字不应回退重打
    c.append_text("好的，我来处理。")
    QTest.qWait(60)
    print("[2] 重渲染后：", sample(c), flush=True)
    QTest.qWait(80)
    print("[2] 重渲染后 +80ms：", sample(c), flush=True)


main()
print("\ndone", flush=True)
