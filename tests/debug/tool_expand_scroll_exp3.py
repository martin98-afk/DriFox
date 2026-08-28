# -*- coding: utf-8 -*-
"""Exp3：复现「简洁坞态 + 正文内展开的编辑类工具框」下的流式滚动乱弹。

背景（前两轮实验结论）：
- Exp1/2（工具块放 #tool-content 内）证明页面内三套保护均正常 → 差异不在工具区窄条。
- 生产代码：简洁模式下 write/edit/multi_edit 等「编辑类工具」保留在正文
  #content-placeholder 中（_inject_tool_streaming_html: _edit_tools 判定
  data-keep-in-content），其 cm-collapsible 可被用户点开为大体积展开块。

本实验完全对齐生产结构：
1. dock on，cp 内预置「展开态编辑类工具块」（大 diff 内容）
2. 持续灌入流式 chunks（差量+全量交替，触发 updateContent/updateTailHtml/
   reorganizeContent/restoreCollapsibleStates 真实链路）
3. 各阶段分别采样 cpTop/cpMax/userUp/bodyTop
4. 对比「无工具框 / 收起 / 展开」三种形态下用户的阅读位置是否可预测保持

运行：EXP_IDX=3 python tests/debug/tool_expand_scroll_repro.py
"""

import os
import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.core.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import CodeWebViewer  # noqa: E402

PROBE = """
(function() {
    var cp = document.getElementById('content-placeholder');
    return {
        cpTop: cp ? Math.round(cp.scrollTop) : -1,
        cpMax: cp ? Math.round(cp.scrollHeight - cp.clientHeight) : -1,
        userUp: cp ? !!cp._userScrolledUp : null,
        within: !!window._userScrolledWithin,
        bTop: Math.round(document.body.scrollTop),
        bMax: Math.round(document.body.scrollHeight - document.body.clientHeight),
        docH: Math.round(document.documentElement.scrollHeight)
    };
})()
"""

_FAT = (
    "流式输出期间内容持续增长，正文容器在限制高度内自行滚动以便跟随最新输出。"
    "段落足够长以触发滚动溢出，需要多行文本才能超过容器最大高度限制形成有效滚动范围。"
) * 4

# 编辑类工具块（生产 data-keep-in-content=true 语义：留在正文内）
_EDIT_BLOCK_EXPANDED = (
    '<div class="tool-block" data-tool-call-id="t-edit1" data-streaming="false" '
    'data-order="5" data-expanded="true" data-keep-in-content="true">'
    '<details class="cm-collapsible" open><summary class="cm-collapsible__summary">✔ edit · main.py</summary>'
    '<div class="cm-collapsible__body"><pre>{lines}</pre></div>'
    "</details></div>"
)
_EDIT_BLOCK_COLLAPSED = (
    '<div class="tool-block" data-tool-call-id="t-edit1" data-streaming="false" '
    'data-order="5" data-expanded="false" data-keep-in-content="true">'
    '<details class="cm-collapsible"><summary class="cm-collapsible__summary">✔ edit · main.py</summary>'
    '<div class="cm-collapsible__body" style="height:0;opacity:0;overflow:hidden"><pre>x</pre></div>'
    "</details></div>"
)

samples: list[tuple[str, dict]] = []


def probe(viewer, label):
    viewer.page().runJavaScript(
        PROBE, lambda r=None, l=label: samples.append((l, r)) or print_row(l, r)
    )


def print_row(label, r):
    print(
        f"{label:24s} bTop={r['bTop']:5d}/{r['bMax']:<5d} cpTop={r['cpTop']:5d}/{r['cpMax']:<5d} "
        f"userUp={str(r['userUp']):5s} within={str(r['within']):5s} docH={r['docH']:5d}"
    )


def main():
    app = QApplication(sys.argv)
    init_shared_web_profile()
    viewer = CodeWebViewer(light=True)
    viewer.resize(620, 700)
    viewer.show()

    state = {"phase": "wait", "n": 0}

    lines = "\n".join([f"+ line {i}: def handle_request(payload): return transform(payload)" for i in range(60)])
    exp_idx = os.environ.get("EXP_IDX", "3")
    block = ""
    if exp_idx == "4":
        block = _EDIT_BLOCK_COLLAPSED.replace("{lines}", "")
    elif exp_idx == "3":
        block = _EDIT_BLOCK_EXPANDED.replace("{lines}", lines)
    label = {3: "正文内工具框【展开】", 4: "正文内工具框【收起】"}.get(int(exp_idx), "?")
    print(f"[exp] EXP_IDX={exp_idx} → {label}")

    # 注入方式：直接塞进骨架 HTML? 骨架由 CodeWebViewer 管理；
    # 走生产等价入口：_inject_tool_streaming_html 需要 MessageCard 全套，
    # 此处退而求其次用 js 注入 cp 开头（并打 data-order 保排序一致），
    # 之后所有 chunk 均走 append_chunk 差量/全量渲染（生产同一函数链）。
    inject_js = (
        "window._toolCompactMode=true; _setStreamingDock(true);"
        "var ts=document.getElementById('tool-section'); ts.style.display='';"
        "ts.setAttribute('data-collapsed','false');"
        f"var cp=document.getElementById('content-placeholder');"
        f"var tmp=document.createElement('div'); tmp.innerHTML='{block}';"
        "cp.insertBefore(tmp.firstElementChild, cp.firstChild);"
        "window._log=[];"
        "['updateContent','updateContentAppend','updateTailHtml','reorganizeContent'].forEach(function(n){"
        " var o=window[n]; if(typeof o==='function'){window[n]=function(){"
        "  var cp=document.getElementById('content-placeholder');"
        "  window._log.push([performance.now()|0,'CALL:'+n,'cp='+Math.round(cp.scrollTop),"
        "  'userUp='+!!cp._userScrolledUp]); return o.apply(this,arguments);};}});"
        "var cpe=document.getElementById('content-placeholder');"
        "cpe.addEventListener('scroll',function(){window._log.push([performance.now()|0,'CP-SCROLL',"
        "Math.round(this.scrollTop),'userUp='+!!this._userScrolledUp,'sup='+!!window._suppressScrollEvent]);});"
    )

    def on_ready():
        viewer.page().runJavaScript(inject_js)
        QTimer.singleShot(250, stream_in)

    def stream_in():
        if state["n"] < 10:
            viewer.append_chunk(_FAT[:110] + f" 第{state['n']}段\n\n")
            state["n"] += 1
            QTimer.singleShot(150, stream_in)
            if state["n"] == 10:
                QTimer.singleShot(500, baseline_up)

    def baseline_up():
        # 用户上滚阅读历史：wheel 冒泡置位 + 实际滚动视口
        # （落点：body 文档级内滚优先；cp 自身溢出时同样上移）
        viewer.page().runJavaScript(
            "var cp=document.getElementById('content-placeholder');"
            "cp.dispatchEvent(new WheelEvent('wheel',{deltaY:-120,bubbles:true}));"
            "if(document.body.scrollHeight>document.body.clientHeight){"
            "  document.body.scrollTop=Math.max(0,document.body.scrollTop-900);}"
            "if(cp.scrollHeight>cp.clientHeight){"
            "  cp.scrollTop=Math.max(0,cp.scrollTop-900);}"
        )
        QTimer.singleShot(200, lambda: probe(viewer, "after-user-up"))
        QTimer.singleShot(400, keep_streaming)

    def keep_streaming():
        if state["n"] < 18:
            if state["n"] % 2 == 1:
                viewer._needs_full_render = True
            viewer.append_chunk(_FAT[:100] + f" 追加{state['n']}\n\n")
            state["n"] += 1
            probe(viewer, f"upd{state['n']}")
            QTimer.singleShot(240, keep_streaming)
        else:
            QTimer.singleShot(400, dump)

    def dump():
        viewer.page().runJavaScript(
            "JSON.stringify(window._log||[])",
            lambda s: (dump_log(s), app.quit()),
        )

    def dump_log(s):
        import json as _json

        print("\n===== JS 轨迹 =====")
        try:
            rows = _json.loads(s or "[]")
            prev = None
            for row in rows:
                txt = " ".join(str(x) for x in row[1:])
                print(txt)
                prev = row
        except Exception as e:
            print("parse fail:", e, str(s)[:300])
        app.quit()

    def sys_argv():
        import sys as _s

        return _s.argv

    viewer.loadFinished.connect(lambda ok: ok and QTimer.singleShot(700, on_ready))
    QTimer.singleShot(30000, app.quit)
    app.exec()
    print("done")


if __name__ == "__main__":
    main()
