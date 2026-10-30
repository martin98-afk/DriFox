# -*- coding: utf-8 -*-
"""v2 简洁模式：搬移稳定性验证（每批不重建 / 不累积）。

背景（2026-10-30 用户反馈）：v2 渲染 + 简洁模式下，工具完成框与思考卡在流式
输出期间「只要流式一更新就闪烁」，右侧滚动条乱跳，判定为「内容/高度抖动」。

根因：``_v2AfterPatch`` 紧凑模式搬移段原实现 = 每批先把工具区搬入节点全删、
再从正文重放。活动 text 块走 live 纯文本尾态时正文没有协议卡 → 工具区清空；
下一次正式化整块重渲又把卡搬回 → 完成框/思考卡每批一删一加（抖动 + 重排）。

修复：搬移改「身份复用」（同 data-tool-call-id / data-flip-key 保留原节点，
正文重渲出的新副本直接丢弃），删除语义延后到非流式终态。

本探针用 text 块内嵌 ``<think>`` + ``<tool>`` 协议段，每批正式化后读工具区：
  - kids 的 pid 保持同一批 → 节点未被重建
  - n 恒定 → 没有累积副本
  - removed 不增长 → 没有每批删除

运行：python tests/debug/_probe_v2_tool_flicker.py
"""

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


def spin(seconds: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec_()


INSTALL_MUT = """
(function () {
  var tc = document.getElementById('tool-content');
  if (!tc) return 'no-tc';
  if (!window.__probeInit) {
    window.__probeInit = true;
    window.__probeAdded = 0;
    window.__probeRemoved = 0;
    var mo = new MutationObserver(function (recs) {
      for (var i = 0; i < recs.length; i++) {
        window.__probeAdded += recs[i].addedNodes.length;
        window.__probeRemoved += recs[i].removedNodes.length;
      }
    });
    mo.observe(tc, { childList: true });
  }
  return 'ok';
})()
"""

READ_STATE = """
(function () {
  var tc = document.getElementById('tool-content');
  if (!tc) return JSON.stringify({err: 'no-tc'});
  var out = [];
  for (var i = 0; i < tc.children.length; i++) {
    var el = tc.children[i];
    if (!el.getAttribute('data-probe-id')) {
      window.__probeSeq = (window.__probeSeq || 0) + 1;
      el.setAttribute('data-probe-id', String(window.__probeSeq));
    }
    out.push({
      pid: el.getAttribute('data-probe-id'),
      cls: String(el.className || '').slice(0, 24),
      tid: (el.getAttribute('data-tool-call-id') || '').slice(0, 16),
      h: Math.round(el.getBoundingClientRect().height)
    });
  }
  return JSON.stringify({
    n: tc.children.length,
    added: window.__probeAdded || 0,
    removed: window.__probeRemoved || 0,
    tcH: tc.scrollHeight,
    kids: out
  });
})()
"""

THINK = "<think>\n这是一段已完成的思考内容，长度固定。\n</think>\n\n"
PROTO = (
    "<tool>\n"
    "name: bash\n"
    'args: {"command": "echo hi"}\n'
    "result: hi\n"
    "success: true\n"
    "tool_call_id: call_probe_1\n"
    "</tool>\n\n"
)
TAIL = "工具/思考之后模型继续输出的正文段落{idx}，用来驱动每批渲染。"


def main() -> None:
    from app.core.conversation.message_content import content_to_markdown
    from app.widgets.card_viewers import CodeWebViewer
    from app.widgets.message_card import MessageCard

    CodeWebViewer._tool_compact_mode = property(lambda self: True)

    card = MessageCard(role="assistant")
    card.resize(760, 520)
    card._content_data = [{"type": "text", "text": "开始。" + THINK + PROTO + TAIL.format(idx=0)}]
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
    viewer.sync_compact_mode_to_js()
    viewer._needs_full_render = True
    viewer._schedule_render(immediate=True)
    spin(0.8)

    def js(code):
        box = []
        viewer.page().runJavaScript(code, lambda r: box.append(r))
        spin(0.3)
        return box[0] if box else None

    js(INSTALL_MUT)
    print(f"[init] {js(READ_STATE)}")

    def render_batch(idx: int, label: str):
        text = "开始。" + THINK + PROTO + TAIL.format(idx=idx)
        card._content_data = [{"type": "text", "text": text}]
        viewer._markdown_text = content_to_markdown(card._content_data)
        viewer._needs_full_render = False
        # 模拟安全边界正式化：活动块整块重渲（协议卡重新产出）
        viewer._v2_live_finalize_requested = True
        viewer._schedule_render(immediate=True)
        spin(0.45)
        print(f"{label}: {js(READ_STATE)}")

    for i in range(1, 7):
        render_batch(i, f"batch{i}")

    print("\n判据：pid 不变 + n 恒定 + removed 不增长 = 搬移稳定（无闪烁/无累积）")


if __name__ == "__main__":
    main()
