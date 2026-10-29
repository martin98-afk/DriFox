# -*- coding: utf-8 -*-
"""流式协议卡归位 probe：活动 text 块每批重灌，验证 _v2AfterPatch 的
「全删重放」搬移不累积重复副本。

场景：简洁模式（compact）下，text 块内嵌 <think> 协议段：
  第 1 批：'前文 <think>思考A</think>'            → 工具区 1 张卡
  第 2 批：'前文 <think>思考A</think> 中 <think>思考B</think>' → 工具区 2 张（非 3）
  第 3 批：删掉思考A（'中 <think>思考B</think>'）  → 工具区 1 张（删除语义生效）
每批检查正文容器协议卡残留数（应为 0）。
"""

import os
import sqlite3
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


JS_COUNT = """
(function () {
  function deepCount(id) {
    var el = document.getElementById(id);
    if (!el) return -1;
    return el.querySelectorAll('.think-block, .think-streaming, .think-compact').length;
  }
  function topStrays(id) {
    var el = document.getElementById(id);
    if (!el) return -1;
    var n = 0;
    for (var i = 0; i < el.children.length; i++) {
      var k = el.children[i];
      if (k.hasAttribute && k.hasAttribute('data-block-key')) continue;
      if (String(k.className || '').match(/think-block|think-streaming|think-compact/)) n++;
      else if (k.getAttribute && k.getAttribute('data-tool-call-id') !== null) n++;
    }
    return n;
  }
  return [
    String(deepCount('content-placeholder')),
    String(deepCount('tool-content')),
    String(topStrays('tool-content')),
    String(window._toolCompactMode)
  ].join('|');
})()
"""

TEXT_V1 = "前文段落。<think>思考A的内容</think>"
TEXT_V2 = "前文段落。<think>思考A的内容</think>中段新增。<think>思考B的内容</think>"
TEXT_V3 = "前文段落。中段新增。<think>思考B的内容</think>"


def main() -> None:
    from app.widgets.message_card import MessageCard

    card = MessageCard(role="assistant")
    card.resize(700, 400)
    card._content_data = [{"type": "text", "text": TEXT_V1}]
    card._streaming = True
    card.show()
    card.ensure_rendered()
    spin(0.3)

    viewer = card.viewer
    if viewer is None or not hasattr(viewer, "_render_v2"):
        print("viewer 未就绪")
        return
    viewer._dock_sync_requested = True  # 跳过坞态分支（probe 环境无坞态链）
    from app.core.conversation.message_content import content_to_markdown

    viewer._streaming = True

    def render_batch(text: str, label: str):
        card._content_data = [{"type": "text", "text": text}]
        viewer._markdown_text = content_to_markdown(card._content_data)
        viewer._needs_full_render = False  # 非首帧：走 diff（活动块重灌路径）
        viewer._v2_last = None  # 强制 force_full 亦可，两种都验 → 先 diff 路径
        viewer._schedule_render(immediate=True)
        spin(1.2)
        results = []
        if viewer.page() is not None:
            viewer.page().runJavaScript(JS_COUNT, lambda r: results.append(r))
            spin(0.6)
        parts = str(results[0] if results else "?|?|?|?").split("|")
        print(f"{label}: cp_deep={parts[0]} tc_deep={parts[1]} tc_top={parts[2]} compactMode={parts[3]}")

    viewer._needs_full_render = True
    render_batch(TEXT_V1, "batch1(v1 全量)")
    viewer._v2_last = None  # 第 2 批仍走全量（模拟思考卡随全量回流的最坏情形）
    render_batch(TEXT_V2, "batch2(v2 全量+新增B)")
    viewer._v2_last = None
    render_batch(TEXT_V3, "batch3(v3 全量+删A)")
    # diff 路径（活动块就地重灌）
    viewer._v2_last = None
    render_batch(TEXT_V2, "batch4(diff 回到 v2)")
    spin(0.5)


if __name__ == "__main__":
    main()
