# -*- coding: utf-8 -*-
"""工具运行框 → 完成框 的 DOM 连续性探针（v2 通道）。

症状（2026-10-30 用户反馈）：工具运行框出现后，到工具完成框出现之间会「消失一阵」，
视觉上工具区空了一下才出现完成框。

本探针按真实时序驱动：
  1. ``update_tool_streaming`` 注入运行框（JS 注入，data-streaming=true）
  2. 若干批正文增长
  3. ``append_tool_result`` 完成（Python 块 + JS 就地转换 + v2 patch）
并在 30ms 间隔采样 ``#tool-content`` 的子节点快照，检查是否存在
「运行框已消失、完成框尚未出现」的空窗帧。

运行：python tests/debug/_probe_tool_run_to_done_gap.py
"""

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DRIFOX_DATA_DIR", str(_ROOT / ".drifox-test"))
os.environ.pop("QT_QPA_PLATFORM", None)

from PyQt5.QtCore import QCoreApplication, QEventLoop, Qt, QTimer  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

apply_render_env(default_config_path())

app = QApplication.instance() or QApplication(sys.argv)

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile(parent=app)

TID = "call_gap_1"

SNAP_JS = """
(function () {
  var tc = document.getElementById('tool-content');
  if (!tc) return JSON.stringify({err: 'no-tc'});
  var out = [];
  for (var i = 0; i < tc.children.length; i++) {
    var c = tc.children[i];
    var inner = c.querySelector('.tool-block, .think-block, .think-compact, .tool-streaming-block') || c;
    out.push({
      w: c.hasAttribute && c.hasAttribute('data-block-wrapper') ? 1 : 0,
      cls: String((inner && inner.className) || '').slice(0, 22),
      tid: (inner && inner.getAttribute && inner.getAttribute('data-tool-call-id')) || '',
      run: (inner && inner.getAttribute && inner.getAttribute('data-streaming')) === 'true' ? 1 : 0,
      h: Math.round(c.getBoundingClientRect ? c.getBoundingClientRect().height : 0)
    });
  }
  return JSON.stringify({n: tc.children.length, items: out});
})()
"""


def spin(seconds: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec_()


def main() -> None:
    from app.widgets.message_card import MessageCard

    card = MessageCard(role="assistant")
    card.resize(820, 560)
    # 工具区先有一个思考块（紧凑模式 home=tool），复现「思考 + 运行框」的真实形态
    card._content_data = [{"type": "reasoning", "content": "先想一下再调工具。"}]
    card._streaming = True
    card.show()
    card.ensure_rendered()
    spin(0.6)

    viewer = card.viewer
    if viewer is None or not getattr(viewer, "_is_js_ready", False):
        print("[FAIL] viewer 未就绪")
        return
    viewer._streaming = True
    viewer._dock_sync_requested = True

    card.start_streaming_anim()
    spin(0.2)
    card.append_text("先给一段正文，然后开始调工具。\n\n")
    spin(0.3)

    # 1) 运行框注入
    card.update_tool_streaming(TID, "bash", {"command": "echo hi"})
    spin(0.6)

    samples = []

    def snap(tag: str):
        box = []
        pg = viewer.page()
        if pg is None:
            samples.append((tag, {"err": "no-page"}))
            return
        pg.runJavaScript(SNAP_JS, lambda r: box.append(r))
        spin(0.2)
        raw = box[0] if box else "{}"
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": str(raw)[:120]}
        samples.append((tag, data))

    snap("t=run-visible")
    print(
        "  [python] content_data types:",
        [b.get("type") for b in card._content_data if isinstance(b, dict)],
        "| viewer._render_v2=", getattr(viewer, "_render_v2", None),
    )

    # 2) 正文继续增长几批（模拟模型边流式边等工具）
    for i in range(3):
        card.append_text(f"工具执行期间模型继续输出的第 {i} 段正文。\n\n")
        spin(0.25)
    snap("t=before-done")

    # 3) 工具完成
    card.append_tool_result(
        tool_name="bash",
        arguments={"command": "echo hi"},
        result="hi\n",
        success=True,
        tool_call_id=TID,
    )
    print(
        "  [python] after done types:",
        [b.get("type") for b in card._content_data if isinstance(b, dict)],
    )
    # 高频采样 1.6s，覆盖「完成信号 → 引擎下一批 patch」窗口
    for i in range(32):
        spin(0.05)
        snap(f"t=+{int((i + 1) * 50)}ms")

    print("\n===== 采样序列 =====")
    prev_n = None
    for tag, data in samples:
        items = data.get("items", [])
        desc = " | ".join(f"{it['cls'][:14]}{'[run]' if it.get('run') else ''}:{it['h']}px" for it in items)
        flag = ""
        n = data.get("n", -1)
        if prev_n is not None and n != prev_n:
            flag = f"  ⟵ children {prev_n}→{n}"
        prev_n = n
        print(f"{tag:>14}: n={n} {desc}{flag}")

    print("\n判据：出现「没有 run 且没有完成框」的连续帧（n 变小且无 tool-block）即空窗")


if __name__ == "__main__":
    main()
