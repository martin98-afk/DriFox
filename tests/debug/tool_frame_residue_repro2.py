# -*- coding: utf-8 -*-
"""复现 v2：长正文（异步/差量渲染路径）下编辑工具完成框的位置。

v1 结论：短正文（1-3 段）下完成框位置正确（正文1 → 完成框 → 正文2），
坞态归位正常。故选 v2 放大内容规模，覆盖差量渲染 / 异步渲染路径。

运行：python tests/debug/tool_frame_residue_repro2.py
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
mc._edit_tools = lambda: frozenset({"write", "edit", "multi_edit", "subagent_para", "question"})

PROBE_JS = """
(function(){
  function desc(el, i){
    var s = (el.tagName||'') + '.' + (el.className||'').slice(0, 42) +
      ' #' + i + ' tcid=' + (el.getAttribute('data-tool-call-id')||'-') +
      ' kic=' + (el.getAttribute('data-keep-in-content')||'-') +
      ' rf=' + (el.getAttribute('data-restored-finished')||'-');
    var t = (el.textContent||'').replace(/\\s+/g,' ').slice(0, 28);
    return s + ' :: ' + t;
  }
  var cp = document.getElementById('content-placeholder');
  var tc = document.getElementById('tool-content');
  var frame = document.querySelector('[data-tool-call-id="CALL_ID"]');
  return JSON.stringify({
    dock: document.body.classList.contains('streaming-dock'),
    cpChildren: cp ? Array.prototype.map.call(cp.children, desc) : null,
    tcChildren: tc ? Array.prototype.map.call(tc.children, desc) : null,
    frameParent: frame && frame.parentNode ? (frame.parentNode.id || '-') : null,
    frameIdx: (frame && frame.parentNode) ? Array.prototype.indexOf.call(frame.parentNode.children, frame) : -1,
    frameSibCount: (frame && frame.parentNode) ? frame.parentNode.children.length : -1,
    frameRestoredFinished: frame ? (frame.getAttribute('data-restored-finished')||'-') : '-',
    cpTextLen: cp ? (cp.textContent||'').length : -1,
    bodyH: document.body.scrollHeight
  }, null, 1);
})()
""".replace("CALL_ID", CALL)


def probe(card, label):
    try:
        raw = card.viewer._run_js_sync(PROBE_JS, timeout_ms=4000)
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(f"--- {label}: probe 失败 {e}", flush=True)
        return None
    print(f"--- {label}", flush=True)
    print(f"    dock={data['dock']} 正文文本长度={data['cpTextLen']} bodyH={data['bodyH']}", flush=True)
    for c in data["cpChildren"] or []:
        print("      cp:", c, flush=True)
    for c in data["tcChildren"] or []:
        print("      tc:", c, flush=True)
    print(
        f"    完成框: parent={data['frameParent']} idx={data['frameIdx']}/{data['frameSibCount']}"
        f" restored-finished={data['frameRestoredFinished']}",
        flush=True,
    )
    return data


def wait_js_ready(card, timeout_ms=15000):
    waited = 0
    while waited < timeout_ms:
        QTest.qWait(200)
        waited += 200
        if getattr(card.viewer, "_is_js_ready", False):
            return True
    return False


PARA = (
    "这是第{n}段正文，用于撑开正文容器并触发差量渲染路径，段落需要足够长才能越过硬边界判断。"
    "内容本身没有意义，只是重复的填充文本，确保容器高度和滚动范围稳定增长，方便观察工具块位置。"
)


def dump_py_state(card, label):
    """打印 Python 端状态：_content_data 块顺序 + 渲染用 markdown 中工具块位置。"""
    try:
        cd = card._content_data or []
        seq = []
        for b in cd:
            if isinstance(b, dict):
                t = b.get("type")
                if t == "tool_result":
                    seq.append("tool:" + str(b.get("tool_name")))
                elif t == "text":
                    seq.append(f"text({len(str(b.get('text', ''))) })")
                else:
                    seq.append(str(t))
            else:
                seq.append("str")
        print(f"    [py {label}] _content_data: {seq}", flush=True)
        md = getattr(card.viewer, "_markdown_text", None) or ""
        print(
            f"    [py {label}] md 长度={len(md)} tool 块位置={md.find('<tool')} "
            f"(末尾={len(md) - 400})",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"    [py {label}] 状态导出失败: {e}", flush=True)


def main():
    card = MessageCard("assistant")
    card.resize(900, 700)
    card.show()
    card.ensure_rendered()
    print("js_ready:", wait_js_ready(card), flush=True)
    card.viewer._toolCompactMode = True
    card.viewer.page().runJavaScript("window._toolCompactMode=true;")
    QTest.qWait(300)

    card.start_streaming_anim()
    QTest.qWait(150)

    # 长正文：每段独立 append，模拟真实分片到达
    for i in range(1, 13):
        card.append_text(PARA.format(n=i) + "\n\n")
        QTest.qWait(120)
    QTest.qWait(500)
    probe(card, "① 长正文流式后（12 段）")

    card.update_tool_streaming(CALL, "write", {"_status": "loading", "_args_len": 32, "_path": "D:/work/novel.txt"})
    QTest.qWait(250)
    card.update_tool_streaming(
        CALL,
        "write",
        {"_status": "loading", "_args_len": 86000, "_path": "D:/work/novel.txt", "_add_lines": 1500, "_del_lines": 0},
    )
    QTest.qWait(400)
    probe(card, "② write 参数流式（长参数）")

    card.finish_streaming()
    QTest.qWait(900)
    probe(card, "③ 文本先结束（keep_dock）")

    card.append_tool_result(
        "write",
        {"path": "D:/work/novel.txt"},
        "已写入 D:/work/novel.txt（100000 字）",
        True,
        CALL,
        diff="--- a/novel.txt\n+++ b/novel.txt\n@@\n+第一段…\n+最后一段…",
    )
    QTest.qWait(900)
    data4 = probe(card, "④ 工具结果落地（完成框）")
    dump_py_state(card, "④ 之后")

    # 工具完成后继续流式正文 —— 观察完成框是否被甩到正文之后
    card.start_streaming_anim()
    QTest.qWait(150)
    for i in range(13, 25):
        card.append_text(PARA.format(n=i) + "\n\n")
        QTest.qWait(120)
    QTest.qWait(700)
    probe(card, "⑤ 后续 12 段正文追加后")

    card.finish_streaming()
    QTest.qWait(900)
    probe(card, "⑥ 收尾全量渲染后")

    # 强触发一次全量重渲染
    card.viewer._schedule_render(immediate=True)
    QTest.qWait(1200)
    probe(card, "⑦ 强制全量重渲染后")

    print("\ndone", flush=True)


main()
