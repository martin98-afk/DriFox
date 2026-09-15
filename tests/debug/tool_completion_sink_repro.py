# -*- coding: utf-8 -*-
"""复现：简洁模式（坞态）下 S1 场景（正文先于工具完成）末轮工具完成框沉底。

时序（生产真实链路）：
1. 简洁模式开 → 流式开始
2. 正文流式输出 → 工具调用（运行中块注入 #tool-content）
3. 继续流式（触发 updateContent → reorganizeContent → save/restore 真实链路）
4. 文本先结束：finish_streaming（有活跃工具 → keep_dock=True，坞态保留）
5. 工具完成：append_tool_result（增量注入完成块）
6. 采样 #tool-content 子元素物理顺序 vs data-order

判定：完成块（tool_1）的物理位置若在列表末尾而其 data-order 非最大 → 沉底复现。

运行：python tests/debug/tool_completion_sink_repro.py
"""

import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.core.webengine_profile import init_shared_web_profile  # noqa: E402
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
              (k.className||'').slice(0,60)]);
  }
  return {order: out};
})()
"""


def main() -> None:
    app = QApplication(sys.argv)
    init_shared_web_profile()

    card = MessageCard(role="assistant")
    card.resize(700, 520)
    card.show()

    def start() -> None:
        card.start_streaming_anim()
        card.ensure_rendered()
        QTimer.singleShot(300, wait_js)

    def wait_js() -> None:
        v = card.viewer
        if v is None or not getattr(v, "_is_js_ready", False):
            QTimer.singleShot(200, wait_js)
            return
        # 简洁模式开关（生产由 Settings.ui_compact_tool_area 注入）
        v.page().runJavaScript("window._toolCompactMode=true; window._setStreamingDock(true);")
        # 数据层：思考1 → 工具1 → 正文 → 思考2 → 工具2（交错 + 锚点偏移）
        card.append_text("开始处理任务。\n\n")
        card.start_new_thinking_block()
        card.append_reasoning("先思考第一步：检索资料。")
        card.update_tool_streaming("tool_1", "search", {"q": "DriFox 滚动锚定"})
        card.append_text("检索进行中，先说明思路。\n\n")
        card.start_new_thinking_block()
        card.append_reasoning("第二步：读取文件确认细节。")
        card.update_tool_streaming("tool_2", "read", {"path": "app/main.py"})
        stream(0)

    def stream(n: int) -> None:
        # 灌 chunk 触发 updateContent → reorganizeContent → save/restore 真实链路
        card.append_text(
            f"流式补充说明第 {n} 段。这段文字足够长，以触发正文重排与工具区迁移逻辑，"
            "并让 save/restore 与排序快路径真实运转。\n\n"
        )
        if n < 3:
            # 60ms 高频间隔：模拟真实流式节奏（渲染走增量分支，reorganizeContent 少跑）
            QTimer.singleShot(60, lambda: stream(n + 1))
        else:
            QTimer.singleShot(400, finish_s1)

    def finish_s1() -> None:
        # S1：文本先于工具完成而结束（有活跃工具 → keep_dock=True）
        card.finish_streaming()
        QTimer.singleShot(700, tool2_done)

    def tool2_done() -> None:
        # 反序完成：后调用的 tool_2 先完成（生产常见：耗时不同的并行工具）
        card.append_tool_result(
            tool_name="read",
            result="文件内容：main.py ...",
            tool_call_id="tool_2",
        )
        QTimer.singleShot(900, tool1_done)

    def tool1_done() -> None:
        card.append_tool_result(
            tool_name="search",
            result="检索完成：DriFox 是一个 PyQt5 桌面 LLM 聊天应用。",
            tool_call_id="tool_1",
        )
        QTimer.singleShot(1200, probe)

    def probe() -> None:
        v = card.viewer
        if v is None:
            print("viewer 缺失")
            app.quit()
            return
        v.page().runJavaScript(PROBE, lambda r=None: (report(r), app.quit()))

    def report(r) -> None:
        print("===== #tool-content 物理顺序 =====")
        rows = (r or {}).get("order") if isinstance(r, dict) else None
        if rows is None:
            print("probe fail:", r)
            app.quit()
            return
        sink = False
        for i, row in enumerate(rows):
            tid, bk, od, cls = row
            print(f"  [{i}] tid={tid} bk={bk} order={od} cls={cls}")
        # 沉底判定：物理顺序与 order 升序不一致（存在相邻逆序对）
        ods = [float(x[2]) for x in rows if x[2] is not None]
        for a, b in zip(ods, ods[1:]):
            if a > b:
                sink = True
        print()
        if sink:
            print(">>> 复现：完成块沉底（物理顺序与 data-order 升序不一致）")
        else:
            print(">>> 未复现：完成块位置正确")
        app.quit()

    QTimer.singleShot(200, start)
    QTimer.singleShot(30000, app.quit)
    app.exec_()


if __name__ == "__main__":
    main()
