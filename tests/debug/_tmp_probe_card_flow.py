# -*- coding: utf-8 -*-
"""临时探针 2：在探针 1 基础上追加 append_text + JS 探针，看是否 append_text 触发崩溃。"""

import faulthandler
import sys

faulthandler.enable(all_threads=True)

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile(app)
print("profile ok", flush=True)

from app.widgets.message_card import MessageCard  # noqa: E402

card = MessageCard(role="assistant")
card.resize(700, 520)
card.show()
card.start_streaming_anim()
card.ensure_rendered()
print("rendered", flush=True)


def step1() -> None:
    print("step1 enter", flush=True)
    v = card.viewer
    print("got viewer attr", flush=True)
    print("viewer", v, "js_ready", getattr(v, "_is_js_ready", None), flush=True)
    if v is None:
        app.quit()
        return
    card.append_text("开始处理任务。\n\n")
    print("append_text ok", flush=True)
    card.start_new_thinking_block()
    print("think ok", flush=True)
    card.append_reasoning("第一步：检索资料。")
    print("reasoning ok", flush=True)
    card.update_tool_streaming("tool_1", "search", {"q": "DriFox"})
    print("tool streaming ok", flush=True)
    QTimer.singleShot(1200, step2)


def step2() -> None:
    card.append_tool_result(
        tool_name="search", result="检索结果：DriFox 是 PyQt5 桌面应用。", tool_call_id="tool_1"
    )
    print("tool result ok", flush=True)
    QTimer.singleShot(800, step3)


def step3() -> None:
    card.append_text("根据检索结果继续分析。\n\n")
    card.start_new_thinking_block()
    card.append_reasoning("第二步：读取配置确认。")
    card.update_tool_streaming("tool_2", "read", {"path": "app/main.py"})
    print("round2 ok", flush=True)
    QTimer.singleShot(1200, step4)


def step4() -> None:
    card.append_tool_result(tool_name="read", result="文件内容：import sys ...", tool_call_id="tool_2")
    print("tool2 result ok", flush=True)
    QTimer.singleShot(800, step5)


def step5() -> None:
    card.finish_streaming()
    print("finish ok", flush=True)
    QTimer.singleShot(1500, done)


def done() -> None:
    v = card.viewer
    if v is None:
        app.quit()
        return
    js = """
    (function(){
      var tc = document.getElementById('tool-content');
      if(!tc) return 'no tc';
      var out = [];
      for (var i=0;i<tc.children.length;i++){
        var k = tc.children[i];
        out.push(k.getAttribute('data-tool-call-id') + '|' + (k.getAttribute('data-block-key')||'').slice(0,10) + '|' + k.getAttribute('data-order'));
      }
      return out.join(' ;; ');
    })()
    """
    v.page().runJavaScript(js, lambda r=None: (print("DOM:", r, flush=True), app.quit()))


QTimer.singleShot(2500, step1)
QTimer.singleShot(60000, app.quit)
app.exec_()
print("quit ok", flush=True)
sys.stdout.flush()
import os

os._exit(0)
