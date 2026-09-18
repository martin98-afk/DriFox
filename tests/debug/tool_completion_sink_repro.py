# -*- coding: utf-8 -*-
"""复现（v2 多轮）：简洁模式多轮 agent 循环下工具完成框沉底。

时序（贴近生产）：
- 第 1 轮：思考1 → 工具1 调用 → 1.5s 执行窗（正文继续流式）→ 工具1 完成
  → LLM 继续流式（agent loop 新一轮）
- 第 2 轮：思考2 → 工具2 调用 → 0.8s 执行窗 → 工具2 完成 → 收尾
- 每个关键步后采样 #tool-content：物理顺序 vs data-order + 关键属性

运行：python tests/debug/tool_completion_sink_repro.py
"""

import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

PROBE = """
(function(){
  var tc = document.getElementById('tool-content');
  if(!tc) return {err:'no tool-content'};
  var out = [];
  for (var i=0;i<tc.children.length;i++){
    var k = tc.children[i];
    out.push([k.getAttribute('data-tool-call-id'),
              (k.getAttribute('data-block-key')||'').slice(0,10),
              k.getAttribute('data-order'),
              k.getAttribute('data-streaming')||'-',
              (k.className||'').slice(0,44)]);
  }
  out.push(['TC-SCROLL', Math.round(tc.scrollTop), Math.round(tc.scrollHeight-tc.clientHeight), '', '']);
  return {order: out};
})()
"""

_LONG = (
    "工具执行期间正文仍在流式输出，这段说明文字足够长，会触发自然边界渲染与"
    "工具区迁移逻辑，穿插多段以模拟真实的 agent 循环节奏。\n\n"
)


def main() -> None:
    app = QApplication(sys.argv)
    init_shared_web_profile()

    card = MessageCard(role="assistant")
    card.resize(700, 520)
    card.show()

    def probe(label: str) -> None:
        v = card.viewer
        if v is None:
            return
        v.page().runJavaScript(PROBE, lambda r=None, l=label: (report(l, r)))

    def report(label: str, r) -> None:
        rows = (r or {}).get("order") if isinstance(r, dict) else None
        if rows is None:
            print(f"[{label}] probe fail: {r}")
            return
        print(f"----- [{label}] -----")
        for row in rows:
            if row[0] == "TC-SCROLL":
                print(f"  tool-content scroll={row[1]}/{row[2]}")
            else:
                print(f"  tid={row[0]} bk={row[1]} od={row[2]} streaming={row[3]} cls={row[4]}")

    def judge() -> None:
        v = card.viewer
        v.page().runJavaScript(PROBE, lambda r=None: (final(r), app.quit()))

    def final(r) -> None:
        rows = (r or {}).get("order") if isinstance(r, dict) else None
        print("===== 最终态 =====")
        sink = False
        if rows:
            ods = [float(x[2]) for x in rows if x[0] != "TC-SCROLL" and x[2] is not None]
            for a, b in zip(ods, ods[1:]):
                if a > b:
                    sink = True
            for row in rows:
                if row[0] == "TC-SCROLL":
                    print(f"  tool-content scroll={row[1]}/{row[2]}")
                else:
                    print(f"  tid={row[0]} bk={row[1]} od={row[2]} streaming={row[3]} cls={row[4]}")
        print(">>> 复现：沉底" if sink else ">>> 最终态顺序正确")

    def start() -> None:
        card.start_streaming_anim()
        card.ensure_rendered()
        QTimer.singleShot(300, wait_js)

    def wait_js() -> None:
        v = card.viewer
        if v is None or not getattr(v, "_is_js_ready", False):
            QTimer.singleShot(200, wait_js)
            return
        v.page().runJavaScript("window._toolCompactMode=true; window._setStreamingDock(true);")
        card.append_text("开始处理任务。\n\n")
        card.start_new_thinking_block()
        card.append_reasoning("第一步：检索资料。")
        card.update_tool_streaming("tool_1", "search", {"q": "DriFox"})
        exec_window(0, after_tool1_done)

    def exec_window(n: int, done) -> None:
        # 工具执行窗：正文继续流式（渲染链持续运转）
        card.append_text(_LONG)
        if n < 5:
            QTimer.singleShot(120, lambda: exec_window(n + 1, done))
        else:
            QTimer.singleShot(300, done)

    def after_tool1_done() -> None:
        card.append_tool_result(
            tool_name="search", result="检索结果：DriFox 是 PyQt5 桌面应用。", tool_call_id="tool_1"
        )
        QTimer.singleShot(600, lambda: (probe("tool1-完成+0.6s"), QTimer.singleShot(200, round2)))

    def round2() -> None:
        # agent loop 新一轮：LLM 继续输出 + 第二个工具
        card.append_text("根据检索结果继续分析。\n\n")
        card.start_new_thinking_block()
        card.append_reasoning("第二步：读取配置确认。")
        card.update_tool_streaming("tool_2", "read", {"path": "app/main.py"})
        exec_window(0, after_tool2_done)

    def after_tool2_done() -> None:
        card.append_tool_result(
            tool_name="read", result="文件内容：import sys ...", tool_call_id="tool_2"
        )
        QTimer.singleShot(600, lambda: (probe("tool2-完成+0.6s"), QTimer.singleShot(300, finish)))

    def finish() -> None:
        card.finish_streaming()
        QTimer.singleShot(1200, judge)

    QTimer.singleShot(200, start)
    QTimer.singleShot(45000, app.quit)
    app.exec_()


if __name__ == "__main__":
    main()
