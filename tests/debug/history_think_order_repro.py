# -*- coding: utf-8 -*-
"""复现：历史模式（非流式直接灌数据）思考块在 #tool-content 的顺序。

流式到达顺序已验证正确（见 history_think_order_repro.py 流式中 PASS），
本脚本跳过流式，直接以完成态数据构造卡片，模拟历史会话加载。

运行：python tests/debug/history_think_order_repro.py --history
"""

import os
import sys

os.environ.setdefault("DRIFOX_NO_CODEBUDDY_REFRESH", "1")
os.environ.setdefault(
    "DRIFOX_DATA_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".drifox-test"),
)

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
    var inner = k.querySelector('.think-block,.think-compact,.think-streaming,.tool-block,.tool-streaming-block');
    var kind = 'other';
    if (inner) {
      var cls = inner.className || '';
      kind = cls.indexOf('think') >= 0 ? 'THINK' : 'TOOL';
    }
    out.push([kind, (k.querySelector('.think-block__summary,.tool-block__summary,.think-compact')||{textContent:''}).textContent.slice(0,14)]);
  }
  return {order: out};
})()
"""


def main() -> None:
    app = QApplication(sys.argv)
    init_shared_web_profile()

    card = MessageCard(role="assistant")
    # 完整走真实历史加载函数（与 main_widget 同路径）
    from app.widgets.ui_helpers import render_batch_to_assistant_card

    batch = [
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": "正文一。", "reasoning_content": "思考一：先加载技能再看代码。"},
        {"role": "tool", "name": "写入记忆", "arguments": {"scope": "daily"}, "content": "ok", "tool_call_id": "t1", "success": True},
        {"role": "assistant", "content": "正文二。", "reasoning_content": "思考二：执行命令验证。"},
        {"role": "tool", "name": "PowerShell 执行", "arguments": {"cmd": "ls"}, "content": "done", "tool_call_id": "t2", "success": True},
        {"role": "assistant", "content": "", "reasoning_content": "思考三：收尾记录。"},
    ]
    card.resize(700, 520)
    card.show()

    def wait_js() -> None:
        v = card.viewer
        if v is None:
            try:
                card.ensure_rendered()
            except Exception as e:
                print(f"ensure_rendered 失败: {e}", flush=True)
                os._exit(1)
                return
        if v is None or not getattr(v, "_is_js_ready", False):
            QTimer.singleShot(200, wait_js)
            return
        # 简洁模式（用户截图场景：思考卡进工具区）
        card.viewer.page().runJavaScript("window._toolCompactMode=true;")
        print("[wait_js] ready，调 render_batch", flush=True)
        QTimer.singleShot(100, run_batch)

    def run_batch() -> None:
        from app.widgets.ui_helpers import render_batch_to_assistant_card

        render_batch_to_assistant_card(card, batch, immediate_render=False)
        print("[step] render_batch 完成，1.5s 后 probe", flush=True)
        QTimer.singleShot(1500, probe)

    def probe() -> None:
        print("[step] probe(history)", flush=True)
        card.viewer.page().runJavaScript(PROBE, report)

    def report(r) -> None:
        print("=== #tool-content 顺序（历史模式）===", flush=True)
        order = (r or {}).get("order", [])
        for i, item in enumerate(order):
            print(f"  [{i}] kind={item[0]:<5} text={item[1]}", flush=True)
        kinds = [x[0] for x in order]
        interleaved = kinds == ["THINK", "TOOL", "THINK", "TOOL", "THINK"]
        print(f"期望交错 THINK,TOOL,THINK,TOOL,THINK → 实际 {kinds}", flush=True)
        print("PASS" if interleaved else "FAIL：历史模式思考块未按到达顺序交错", flush=True)
        os._exit(0)

    QTimer.singleShot(300, wait_js)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
