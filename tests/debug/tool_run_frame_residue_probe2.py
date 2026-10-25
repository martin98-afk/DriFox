# -*- coding: utf-8 -*-
"""复现探针：运行框/思考占位在「工具与思考」区的残留（2026-10-25 截图场景）。

用户截图现象（流式进行中，卡片底部工具与思考区）：
- 已完成思考文字块 + 新「深度思考中...」占位并存
- 已完成工具块（绿点）+「读取 ○ 接收中...」运行框并存

本脚本驱动真实 MessageCard（离屏 WebEngine）走交错时序，逐步采样 DOM：
1. 思考1（占位「深度思考中」）
2. 工具1 read（运行框→完成块）
3. 正文
4. 思考2（第二个占位）
5. 工具2 read（运行框→完成块）
6. markdown 未闭合 <tool> 字面文本（「接收中...」占位另一来源）
7. finish_streaming 收尾 → 终态采样

判定：
- 终态 .think-streaming[data-streaming="true"] 应为 0（占位全部定稿）
- 终态 .tool-streaming-block 应为 0（运行框全部转换/清除）
- 同 tool_call_id 块数应恒为 1
"""

import json
import sys

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

init_shared_web_profile()

PROBE_JS = """
(function(){
  function desc(el){
    return '[' + (el.getAttribute('data-tool-call-id')||'') +']' +
      ' cls=' + (el.className||'').toString().slice(0,60) +
      ' stream=' + (el.getAttribute('data-streaming')||'-') +
      ' order=' + (el.getAttribute('data-order')||'-') +
      ' txt=' + (el.textContent||'').replace(/\\s+/g,' ').slice(0,40);
  }
  var ts = document.getElementById('tool-section');
  var tc = document.getElementById('tool-content');
  var cp = document.getElementById('content-placeholder');
  var thinkStream = document.querySelectorAll('.think-streaming[data-streaming="true"]');
  var streamBlocks = document.querySelectorAll('.tool-streaming-block');
  var toolBlocks = document.querySelectorAll('[data-tool-call-id]');
  return JSON.stringify({
    dock: document.body.classList.contains('streaming-dock'),
    thinkStreamingCount: thinkStream.length,
    thinkStreamingText: Array.prototype.map.call(thinkStream, function(e){return (e.textContent||'').slice(0,30);}),
    toolStreamingCount: streamBlocks.length,
    toolBlockCount: toolBlocks.length,
    tcChildren: tc ? Array.prototype.map.call(tc.children, desc) : [],
    cpToolLike: cp ? Array.prototype.map.call(cp.querySelectorAll('[data-tool-call-id],.think-streaming'), desc) : [],
    tsDisplay: ts ? ts.style.display : '-',
    bodyH: document.body.scrollHeight
  }, null, 1);
})()
"""


def probe(card, label):
    try:
        raw = card.viewer._run_js_sync(PROBE_JS, timeout_ms=4000)
    except Exception as e:  # noqa: BLE001
        print(f"--- {label}: probe 失败 {e}", flush=True)
        return
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        print(f"--- {label}: 原始返回 {raw!r}", flush=True)
        return
    print(f"--- {label}", flush=True)
    print(
        f"    dock={data['dock']} thinkStream(占位)={data['thinkStreamingCount']}{data['thinkStreamingText']}"
        f" toolStreaming(运行框)={data['toolStreamingCount']} toolBlocks={data['toolBlockCount']}"
        f" tsDisplay={data['tsDisplay']!r} bodyH={data['bodyH']}",
        flush=True,
    )
    if data["tcChildren"]:
        print("    tool-content:", flush=True)
        for c in data["tcChildren"]:
            print("      ", c, flush=True)
    if data["cpToolLike"]:
        print("    正文区疑似块:", flush=True)
        for c in data["cpToolLike"]:
            print("      ", c, flush=True)


def wait_js_ready(card, timeout_ms=15000):
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
    # 简洁模式（截图场景）：_tool_compact_mode 从 Settings 读取，默认 True；只同步 JS 侧全局
    card.viewer.sync_compact_mode_to_js()
    QTest.qWait(300)

    card.start_streaming_anim()
    QTest.qWait(200)

    # ① 思考1 → 占位
    card.append_reasoning("思考1：先读 card_viewers 相关区域确认坞态逻辑。")
    QTest.qWait(500)
    probe(card, "① 思考1 占位")

    # ② 工具1 read 运行 → 完成
    card.update_tool_streaming("call_r1", "read", {"_status": "loading", "_args_len": 32, "_path": "D:/work/a.py"})
    QTest.qWait(400)
    probe(card, "② 工具1 运行框")
    card.append_tool_result("read", {"path": "D:/work/a.py"}, "# 文件内容 a…", True, "call_r1")
    QTest.qWait(600)
    probe(card, "③ 工具1 完成块")

    # ④ 正文
    card.append_text("根据读取结果，继续排查下一处。")
    QTest.qWait(400)

    # ⑤ 思考2 → 第二个占位（截图中的「深度思考中...」残留候选）
    card.append_reasoning("思考2：再读另一段行号区间验证假设。")
    QTest.qWait(400)
    probe(card, "⑤ 思考2 占位（此时思考1占位应已定稿）")

    # ⑥ 工具2 read 运行 → 完成（截图中的「接收中」残留候选）
    card.update_tool_streaming("call_r2", "read", {"_status": "loading", "_args_len": 0})
    QTest.qWait(400)
    probe(card, "⑥ 工具2 运行框（无参数）")
    card.update_tool_streaming("call_r2", "read", {"_status": "loading", "_args_len": 64, "_path": "D:/work/b.py"})
    QTest.qWait(300)
    card.finish_tool_streaming("call_r2", "read", {"path": "D:/work/b.py"})
    QTest.qWait(300)
    card.append_tool_result("read", {"path": "D:/work/b.py"}, "# 文件内容 b…", True, "call_r2")
    QTest.qWait(700)
    probe(card, "⑦ 工具2 完成块（运行框应被转换）")

    # ⑧ markdown 未闭合 <tool> 字面文本（「接收中...」markdown 渲染来源）
    card.append_text("下面是协议样例字面文本：<tool>\\nname: read\\nargs: {\"path\": \"c.py\"}")
    QTest.qWait(700)
    probe(card, "⑧ 未闭合 <tool> 字面文本（markdown 占位）")

    # ⑨ 收尾
    card.finish_streaming()
    QTest.qWait(1000)
    probe(card, "⑨ finish_streaming 终态")

    # ⑩ 二次收尾（幂等性）
    card.finish_streaming()
    QTest.qWait(800)
    probe(card, "⑩ 二次 finish_streaming")

    print("\n[判定] 终态 thinkStreaming 与 toolStreaming 应为 0", flush=True)


main()
