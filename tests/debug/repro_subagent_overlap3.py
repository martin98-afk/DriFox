# -*- coding: utf-8 -*-
"""复现诊断（第三轮）：真 MessageCard + 真 viewer 跑完整流式工具三步链路。

第二轮证明终渲染（静态）不叠印。本轮重建动态时序：
  update_tool_streaming（运行框注入）
  → finish_tool_streaming（完成预览态）
  → append_tool_result（流式块 → 完成折叠框 outerHTML 替换）
  → 二次 append_tool_result（模拟工具区/终渲染后又一拍更新，走 existing 非
    streaming 的 btn.innerHTML 增量分支）
每步量测 summary 行内元素 rect，抓叠印出现的那一拍。

运行：uv run python tests/debug/repro_subagent_overlap3.py
"""

import json
import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

ARGS = {
    "tasks": [
        {
            "agent": "explore",
            "description": "独立复核 DriFox 消息卡片 HTML 区偶发纯白背景的根因链。已知证据：… + 3~5 条可核对的证据卡",
        }
    ]
}
TID = "call_repro_dyn_1"

MEASURE_JS = """
(function() {
  var tids = document.querySelectorAll('[data-tool-call-id="__TID__"]');
  var out = {blockCount: tids.length, blocks: []};
  function r(el) {
    var b = el.getBoundingClientRect();
    return {x: Math.round(b.x), top: Math.round(b.y), right: Math.round(b.right), bottom: Math.round(b.bottom), w: Math.round(b.width), h: Math.round(b.height)};
  }
  for (var i = 0; i < tids.length; i++) {
    var el = tids[i];
    var info = {
      cls: el.className.toString().slice(0, 70),
      streaming: el.getAttribute('data-streaming'),
      rect: r(el),
      parent: el.parentElement ? ((el.parentElement.id || 'no-id') + '/' + (el.parentElement.className || '').toString().slice(0, 40)) : '?',
      kids: []
    };
    var scope = el; // 流式块与折叠块都从块级元素量子元素
    for (var j = 0; j < scope.children.length; j++) {
      var c = scope.children[j];
      var cs = getComputedStyle(c);
      info.kids.push({
        tag: c.tagName, txt: (c.textContent || '').trim().slice(0, 26),
        rect: r(c), display: cs.display, flex: cs.flex, ml: cs.marginLeft,
        minw: cs.minWidth, overflow: cs.overflow, pos: cs.position,
        transform: cs.transform
      });
      for (var k2 = 0; k2 < c.children.length; k2++) {
        var g = c.children[k2];
        var gs = getComputedStyle(g);
        info.kids.push({
          tag: g.tagName + '(n)', txt: (g.textContent || '').trim().slice(0, 26),
          rect: r(g), display: gs.display, flex: gs.flex, ml: gs.marginLeft,
          minw: gs.minWidth, overflow: gs.overflow, pos: gs.position,
          transform: gs.transform
        });
      }
    }
    out.blocks.push(info);
  }
  return out;
})()
""".replace("__TID__", TID)


def judge(label, res) -> None:
    if not isinstance(res, dict) or "blockCount" not in res:
        print(f"[{label}] 量测失败: {res}")
        return
    print(f"\n===== [{label}] 块数={res['blockCount']} =====")
    for b in res["blocks"]:
        print(f"  cls={b['cls']!r} streaming={b['streaming']} rect={b['rect']} 父={b['parent']!r}")
        name_k = prev_k = None
        for k in b["kids"]:
            print(
                f"    <{k['tag']}> txt={k['txt']!r} rect={k['rect']} display={k['display']} "
                f"ml={k['ml']} minw={k['minw']} pos={k['pos']} tf={k['transform'][:36]}"
            )
            if k["txt"] == "分发任务":
                name_k = k
            if "独立复核" in k["txt"]:
                prev_k = k
        if name_k and prev_k:
            overlap = name_k["rect"]["right"] - prev_k["rect"]["x"]
            print(f"    [judge] cn_name.right={name_k['rect']['right']} preview.x={prev_k['rect']['x']} → {'叠印 ' + str(overlap) + 'px' if overlap > 0 else '正常'}")


def main() -> None:
    app = QApplication(sys.argv)

    from app.core.infra.webengine_profile import init_shared_web_profile

    init_shared_web_profile()

    import app.widgets.message_card as mc
    from app.widgets.message_card import CodeWebViewer, MessageCard

    card = MessageCard(role="assistant")
    card.resize(1400, 400)
    card.show()
    card.ensure_rendered()
    viewer = card.viewer
    assert viewer is not None, "ensure_rendered 后 viewer 仍为空"
    viewer.resize(1400, 620)

    steps = [
        ("1_streaming", lambda: card.update_tool_streaming(tool_call_id=TID, tool_name="subagent_para", partial_args={"tasks": [{}]})),
        ("2_args_full", lambda: card.update_tool_streaming(tool_call_id=TID, tool_name="subagent_para", partial_args=ARGS)),
        ("3_finish_preview", lambda: card.finish_tool_streaming(tool_call_id=TID, tool_name="subagent_para", arguments=ARGS)),
        ("4_append_result", lambda: card.append_tool_result(tool_name="subagent_para", arguments=ARGS, result="(任务已分派)", success=True, tool_call_id=TID)),
        ("5_append_again", lambda: card.append_tool_result(tool_name="subagent_para", arguments=ARGS, result="(任务已分派)", success=True, tool_call_id=TID)),
        ("6_final_render", lambda: _final_render()),
    ]

    def _final_render():
        md = (
            "让一个只读子智能体交叉验证因果链。\n\n"
            "已定位完整因果链，等待独立复核结果。\n\n"
            "<tool>\nname: subagent_para\n"
            'args: ' + json.dumps(ARGS) + "\n"
            "result: (任务已分派)\nsuccess: true\ntool_call_id: " + TID + "\n</tool>\n\n"
            "等待复核结果的同时固定复现条件。\n"
        )
        card._content_data = [{"type": "text", "text": md}]
        viewer._markdown_text = md
        viewer._perform_update()
    state = {"i": 0}

    def measure_then_next() -> None:
        label = steps[state["i"] - 1][0] if state["i"] > 0 else "0_initial"

        def on_measure(res) -> None:
            judge(label, res)
            run_next()

        viewer.page().runJavaScript(MEASURE_JS, 0, on_measure)

    def run_next() -> None:
        if state["i"] >= len(steps):
            print("\n[done] 全部步骤量测完成")
            QTimer.singleShot(100, app.quit)
            return
        fn = steps[state["i"]][1]
        state["i"] += 1
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[step {state['i']}] Python 侧异常: {exc!r}")
        QTimer.singleShot(900, measure_then_next)

    QTimer.singleShot(1500, run_next)
    QTimer.singleShot(30000, app.quit)
    app.exec_()


if __name__ == "__main__":
    main()
