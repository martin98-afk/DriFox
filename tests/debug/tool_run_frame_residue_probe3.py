# -*- coding: utf-8 -*-
"""复现探针 v2：多轮 agentic 循环（持续 streaming）下运行框/思考占位残留。

与 probe2 的差异：真实 worker 在一轮用户消息内做多次 LLM→工具 循环，
卡片全程 streaming，只在最终 done 才 finish_streaming。本脚本按真实信号时序
（thinking_started → reasoning chunks → tool_args → text → tool_result）×N 轮，
chunk 间隔缩短（30-80ms）模拟高频流式，每轮采样关键 DOM 指标。

判定：
- 任意时刻同一 tool_call_id 块数应 = 1
- 终态 .think-streaming[data-streaming="true"] = 0、.tool-streaming-block = 0
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
  var ts = document.getElementById('tool-section');
  var tc = document.getElementById('tool-content');
  var thinkStream = document.querySelectorAll('.think-streaming[data-streaming="true"]');
  var streamBlocks = document.querySelectorAll('.tool-streaming-block');
  var toolBlocks = document.querySelectorAll('[data-tool-call-id]');
  // 同 id 多块检测
  var idCount = {};
  Array.prototype.forEach.call(toolBlocks, function(el){
    var id = el.getAttribute('data-tool-call-id');
    idCount[id] = (idCount[id]||0)+1;
  });
  var dups = Object.keys(idCount).filter(function(k){return idCount[k]>1;});
  function brief(el){
    return '[' + (el.getAttribute('data-tool-call-id')||'') + ']'+
      (el.className||'').toString().replace('tool-block','tb').slice(0,50) +
      ' s=' + (el.getAttribute('data-streaming')||'-') +
      ' o=' + (el.getAttribute('data-order')||'-');
  }
  return JSON.stringify({
    dock: document.body.classList.contains('streaming-dock'),
    nThink: thinkStream.length,
    nStream: streamBlocks.length,
    nTool: toolBlocks.length,
    dups: dups,
    tc: tc ? Array.prototype.map.call(tc.children, brief) : [],
    tsDisp: ts ? ts.style.display : '-'
  });
})()
"""

ALERTS = []


def probe(card, label):
    try:
        raw = card.viewer._run_js_sync(PROBE_JS, timeout_ms=4000)
    except Exception as e:  # noqa: BLE001
        print(f"--- {label}: probe 失败 {e}", flush=True)
        return
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        print(f"--- {label}: 原始 {raw!r}", flush=True)
        return
    flag = ""
    if d["dups"]:
        flag += " ⚠️同id多块:" + ",".join(d["dups"])
        ALERTS.append(f"{label}: 同id多块 {d['dups']}")
    print(
        f"--- {label}: dock={d['dock']} think占位={d['nThink']} 运行框={d['nStream']} 工具块={d['nTool']} ts={d['tsDisp']!r}{flag}",
        flush=True,
    )
    for c in d["tc"]:
        print("      ", c, flush=True)
    return d


def wait_js_ready(card, timeout_ms=15000):
    waited = 0
    while waited < timeout_ms:
        QTest.qWait(50)
        waited += 50
        if getattr(card.viewer, "_is_js_ready", False):
            return True
    return False


def main():
    card = MessageCard("assistant")
    card.resize(900, 700)
    card.show()
    card.ensure_rendered()
    if not wait_js_ready(card):
        print("js 未就绪", flush=True)
        return
    card.viewer.sync_compact_mode_to_js()
    QTest.qWait(200)

    ROUNDS = 5
    for r in range(1, ROUNDS + 1):
        tid = f"call_{r}"
        card.start_streaming_anim()          # 轮次开始（worker 重复触发）
        card.start_new_thinking_block()      # thinking_started
        card.append_reasoning(f"思考{r}：分析问题第 {r} 步。")
        QTest.qWait(30)
        card.update_tool_streaming(tid, "read", {"_status": "loading", "_args_len": 16})
        QTest.qWait(30)
        card.update_tool_streaming(tid, "read", {"_status": "loading", "_args_len": 64, "_path": f"D:/w/{r}.py"})
        QTest.qWait(30)
        card.append_text(f"第 {r} 步正文：继续读取文件验证。")
        QTest.qWait(60)
        card.append_tool_result("read", {"path": f"D:/w/{r}.py"}, f"内容 {r}", True, tid)
        QTest.qWait(120)
        probe(card, f"轮{r} 结束")

    card.finish_streaming()
    QTest.qWait(1000)
    d = probe(card, "终态 finish_streaming")

    if d and (d["nThink"] or d["nStream"] or d["dups"]):
        ALERTS.append(f"终态残留: think={d['nThink']} stream={d['nStream']} dups={d['dups']}")
    print("\n[判定]", "残留复现 ⚠️" if ALERTS else "干净 ✓", flush=True)
    for a in ALERTS:
        print("  -", a, flush=True)


main()
