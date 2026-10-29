# -*- coding: utf-8 -*-
"""临时探针：对比新旧管线产物（不做断言，只看差异规模）。"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DRIFOX_DATA_DIR", str(_ROOT / ".drifox-test"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DRIFOX_QT_RENDERER"] = "1"

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.core.conversation.message_content import content_to_markdown  # noqa: E402
from app.widgets.card_render_core import _render_markdown_to_html_cached  # noqa: E402
from app.widgets.render import RenderCtx, render_message, visible_text  # noqa: E402


def old_render(blocks, ctx):
    md = content_to_markdown(blocks)
    return _render_markdown_to_html_cached(md, compact=ctx.compact, lazy_body=ctx.lazy_body)


CASES = {
    "纯文本两段": [
        {"type": "text", "text": "第一段正文，含 **加粗** 与 `inline code`。"},
        {"type": "text", "text": "第二段正文，含 [链接](https://example.com)。"},
    ],
    "思考+正文": [
        {"type": "reasoning", "content": "这里是推理过程。\n\n第二段推理。"},
        {"type": "text", "text": "结论正文。"},
    ],
    "工具+正文": [
        {"type": "text", "text": "先看工具。"},
        {
            "type": "tool_result",
            "name": "read",
            "arguments": {"path": "/a/b.py"},
            "result": "print(1)",
            "success": True,
            "tool_call_id": "call_1",
        },
        {"type": "text", "text": "工具之后。"},
    ],
    "代码块": [
        {"type": "text", "text": "```python\nprint('hi')\n```"},
    ],
    "协议文本内嵌": [
        {"type": "text", "text": "正文前\n\n<think>内嵌思考</think>\n\n正文后"},
    ],
    "表格": [
        {"type": "text", "text": "| a | b |\n|---|---|\n| 1 | 2 |"},
    ],
}

ctx = RenderCtx(streaming=False, compact=False)
for name, blocks in CASES.items():
    old_html = old_render(blocks, ctx)
    new = render_message(blocks, ctx)
    new_html = "\n".join(it.html for it in new.content)
    ot = visible_text(old_html)
    nt = visible_text(new_html)
    print("=" * 60)
    print(name)
    print("  old len", len(old_html), "new len", len(new_html))
    print("  text equal:", ot == nt)
    if ot != nt:
        print("  OLD:", ot[:300])
        print("  NEW:", nt[:300])
