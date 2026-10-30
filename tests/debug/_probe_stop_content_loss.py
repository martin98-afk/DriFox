# -*- coding: utf-8 -*-
"""强停（中途打断）终渲染复现探针：半截内容在 completed=True 下的渲染产物。

背景：用户报强停后「已渲染正文丢失，只剩工具完成框」。
本探针在纯 Python 层跑 v2 终渲染管线（render_blocks completed=True），
对停止时刻可能的半截内容形态逐一验证正文是否被吞。

只读探针，不改任何状态。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.widgets.render.block_render import (
    RenderCtx,
    block_key,
    render_blocks,
)
from app.widgets.render.message_render import HOME_CONTENT, HOME_TOOL, render_message, visible_text

CASES = {
    "正常闭合正文（对照）": "这是第一段正文。\n\n这是第二段正文，包含结果说明。",
    "半截 echarts fence": "前文说明。\n\n```echarts\n" + '{"title":{"text":"销量"},"series":[{"type":"bar","data":[1,',
    "半截普通代码 fence": "看这段代码：\n\n```python\ndef foo(\n    a,",
    "未闭合 <tool> 协议段": "前文。\n\n<tool>\n{\"name\": \"bash\", \"arguments\": {\"command\": \"ls",
    "未闭合 <think> 协议段": "<think>\n我需要先分析用户的需求，然后",
    "闭合 <think> + 正文": "<think>思考完成。</think>\n\n正文结论在这里。",
}


def run_case(name: str, text: str) -> None:
    ctx = RenderCtx(streaming=False, compact=True, lazy_body=True, heavy_caps=None, tool_fingerprints=frozenset())
    blocks = [{"type": "text", "text": text}]
    mr = render_message(blocks, ctx, active_index=None, char_count=False, cache=None)
    content_texts = [visible_text(it.html) for it in mr.content]
    tool_count = len(mr.tool)
    joined = " | ".join(t for t in content_texts if t)
    print(f"\n=== {name} ===")
    print(f"  content 块数: {len(mr.content)}, 非空可见文本: {joined[:80]!r}")
    print(f"  tool 块数: {tool_count}")
    for it in mr.content:
        if not it.html.strip():
            print(f"  !! 空 content 块: key={it.key[:40]} html_len={len(it.html)}")
    if not joined and tool_count == 0 and not any(it.html.strip() for it in mr.content):
        print("  >>> 正文全空（复现『只剩工具框』的前半条件）")


def main() -> None:
    for name, text in CASES.items():
        run_case(name, text)


if __name__ == "__main__":
    main()
