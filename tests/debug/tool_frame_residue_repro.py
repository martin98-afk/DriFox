# -*- coding: utf-8 -*-
"""复现：编辑工具（write）完成框「残留在正文最底部」。

背景
----
用户反馈：吞框修复后完成框不再丢失，但完成后完成框一直留在正文（正文容器
#content-placeholder）的最底部，未回到工具调用位置 / 未进入「工具与思考」区。

设计事实（代码依据）：
- write/edit 等 keep_in_content 工具的流式块与完成块都注入 #content-placeholder
  （message_card.py:15596 / 15943），不迁入 #tool-content。
- #tool-section 与 #content-placeholder 的相对位置由坞态 CSS order 控制：
  body.streaming-dock → content:1 / tool:2（工具区沉底）；
  非坞态 → 文档顺序（工具区在上方）。

本脚本驱动真实 MessageCard（离屏 WebEngine）走一遍最小序列，逐步采样 DOM：
1. 正文流式（append_text）
2. write 参数流式（update_tool_streaming）→ 运行框注入正文区
3. 文本先结束（finish_streaming，keep_dock=True）
4. 工具完成（append_tool_result）→ 完成块
5. 最终收尾（finish_streaming）

观察点：完成块（data-tool-call-id）落在哪个容器、是否重复、坞态是否归位。

运行：python tests/debug/tool_frame_residue_repro.py
"""

import json
import sys

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)

from app.core.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets import message_card as mc  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

init_shared_web_profile()

CALL = "call_w1"
EDIT = frozenset({"write", "edit", "multi_edit", "subagent_para", "question"})
mc._edit_tools = lambda: EDIT

PROBE_JS = """
(function(){
  function desc(el){
    return (el.tagName||'') + '.' + (el.className||'') +
      ' |tcid=' + (el.getAttribute('data-tool-call-id')||'-') +
      ' |kic=' + (el.getAttribute('data-keep-in-content')||'-') +
      ' |stream=' + (el.getAttribute('data-streaming')||'-') +
      ' |order=' + (el.getAttribute('data-order')||'-');
  }
  var cp = document.getElementById('content-placeholder');
  var tc = document.getElementById('tool-content');
  var ts = document.getElementById('tool-section');
  var frame = document.querySelector('[data-tool-call-id="CALL_ID"]');
  return JSON.stringify({
    dock: document.body.classList.contains('streaming-dock'),
    compact: !!window._toolCompactMode,
    cpChildren: cp ? Array.prototype.map.call(cp.children, desc) : null,
    tcChildren: tc ? Array.prototype.map.call(tc.children, desc) : null,
    tsDisplay: ts ? ts.style.display : null,
    frameParent: frame && frame.parentNode ? (frame.parentNode.id || frame.parentNode.className) : null,
    frameIdx: (frame && frame.parentNode) ? Array.prototype.indexOf.call(frame.parentNode.children, frame) : -1,
    frameSibCount: (frame && frame.parentNode) ? frame.parentNode.children.length : -1,
    dupCount: document.querySelectorAll('[data-tool-call-id="CALL_ID"]').length,
    bodyH: document.body.scrollHeight
  }, null, 1);
})()
""".replace("CALL_ID", CALL)


def probe(card, label):
    try:
        raw = card.viewer._run_js_sync(PROBE_JS, timeout_ms=3000)
    except Exception as e:  # noqa: BLE001
        print(f"--- {label}: probe 失败 {e}", flush=True)
        return
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        print(f"--- {label}: 原始返回 {raw!r}", flush=True)
        return
    print(f"--- {label}", flush=True)
    print(f"    dock={data['dock']} compact={data['compact']} tsDisplay={data['tsDisplay']!r}", flush=True)
    print(f"    content-placeholder 子节点:", flush=True)
    for c in data["cpChildren"] or []:
        print("      ", c, flush=True)
    print(f"    tool-content 子节点:", flush=True)
    for c in data["tcChildren"] or []:
        print("      ", c, flush=True)
    print(
        f"    完成框: parent={data['frameParent']} idx={data['frameIdx']}/{data['frameSibCount']}"
        f" dup={data['dupCount']} bodyH={data['bodyH']}",
        flush=True,
    )


def wait_js_ready(card, timeout_ms=12000):
    waited = 0
    while waited < timeout_ms:
        QTest.qWait(200)
        waited += 200
        if getattr(card.viewer, "_is_js_ready", False):
            return True
    return False


def main():
    card = MessageCard("assistant")
    card.resize(900, 700)
    card.show()
    card.ensure_rendered()
    ok = wait_js_ready(card)
    print("js_ready:", ok, flush=True)
    if not ok:
        return
    # 简洁模式（截图场景）：工具区沉底 + 折叠
    card.viewer._toolCompactMode = True
    card.viewer.page().runJavaScript("window._toolCompactMode=true;")
    QTest.qWait(300)

    card.start_streaming_anim()
    QTest.qWait(200)

    card.append_text("先说结论预期：根据既往测试经验，先落一段正文，再调用 write 工具落盘。")
    QTest.qWait(400)
    probe(card, "① 正文流式后")

    card.update_tool_streaming(CALL, "write", {"_status": "loading", "_args_len": 32, "_path": "D:/work/novel.txt"})
    QTest.qWait(300)
    probe(card, "② write 参数流式（运行框）")

    card.update_tool_streaming(
        CALL,
        "write",
        {"_status": "loading", "_args_len": 52000, "_path": "D:/work/novel.txt", "_add_lines": 900, "_del_lines": 2},
    )
    QTest.qWait(300)
    probe(card, "③ write 参数增长")

    card.finish_tool_streaming(CALL, "write", {"path": "D:/work/novel.txt", "content": "…10 万字…"})
    QTest.qWait(300)
    probe(card, "④ 参数接收完成（完成态预览）")

    card.finish_streaming()
    QTest.qWait(500)
    probe(card, "⑤ 文本先结束（keep_dock 应为 True）")

    card.append_tool_result(
        "write",
        {"path": "D:/work/novel.txt"},
        "已写入 D:/work/novel.txt（100000 字）",
        True,
        CALL,
        diff="--- a/novel.txt\n+++ b/novel.txt\n@@\n+第一段…\n+最后一段…",
    )
    QTest.qWait(600)
    probe(card, "⑥ 工具结果落地（完成框）")

    card.finish_streaming()
    QTest.qWait(800)
    probe(card, "⑦ 最终收尾 finish_streaming")

    # ⑧ 新一轮正文流式：完成框应被后续正文「压在下面」还是继续沉底？
    card.start_streaming_anim()
    QTest.qWait(200)
    card.append_text("补充说明：这次调用只验证容量上限，文学质量不计。")
    QTest.qWait(700)
    probe(card, "⑧ 后续新正文追加后")

    card.finish_streaming()
    QTest.qWait(800)
    probe(card, "⑨ 新正文收尾")

    # ⑩ 触发一次全量渲染（save/restore 路径）——复现关键嫌疑点
    card.viewer._schedule_render(immediate=True)
    QTest.qWait(900)
    probe(card, "⑩ 全量渲染后")

    # ⑪ 再次追加正文
    card.start_streaming_anim()
    QTest.qWait(200)
    card.append_text("第三段：收尾说明，用于观察完成框相对位置是否稳定。")
    QTest.qWait(800)
    probe(card, "⑪ 第三段正文后")

    # ⑫ 第二轮工具调用（同 id 不同轮）——观察第二轮完成后第一轮框是否被挤到末尾
    card.update_tool_streaming("call_w2", "read", {"_status": "loading", "_args_len": 64, "_path": "D:/work/x"})
    QTest.qWait(400)
    probe(card, "⑫ 第二轮工具运行框")
    card.append_tool_result("read", {"path": "D:/work/x"}, "文件内容…", True, "call_w2")
    QTest.qWait(700)
    probe(card, "⑬ 第二轮工具完成")

    card.finish_streaming()
    QTest.qWait(800)
    probe(card, "⑭ 全部收尾")

    print("\ndone", flush=True)


main()
