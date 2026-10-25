# -*- coding: utf-8 -*-
"""全量渲染落地后差量基线推进点回归：stable 不得切进 <tool>/<think> 协议块内部。

背景（2026-10-25 真机「长回复流式反复闪烁 + 更新失效」）：
工具 result/args 文本几乎必含空行，而 _last_para_break_outside_fence 只感知
代码 fence，会把协议块内部空行当段落边界 → _stable_md_len 推进点切进工具块
中间 → tail 永带 ``</tool>`` 残片 → 降载分支（tail 含协议标签让位全量）每次
都让位 → 每次工具完成后 ~300ms 内连发第二次 save/restore 整页替换（闪烁 +
打字机中断）。复现探针
tests/debug/stream_tool_cycle_saverestore_probe3.py 实测 save/restore 间隔
[352, 2697, 347, 2703]（成对出现），修复后应为每工具完成 1 次。

这些测试只测纯函数，不加载页面（不触发 Chromium 导航），无 GPU CI 可运行。
"""

from app.widgets.card_render_core import (
    _compute_stream_stable_len,
    _last_closed_proto_tag_end,
    _last_para_break_outside_fence,
)


class TestLastClosedProtoTagEnd:
    def test_none(self):
        assert _last_closed_proto_tag_end("纯正文没有协议块") == -1

    def test_tool_only(self):
        md = "正文\n\n<tool>\nname: x\n</tool>\n\n尾段"
        assert _last_closed_proto_tag_end(md) == md.index("</tool>") + len("</tool>")

    def test_think_and_tool_take_later(self):
        md = "<think>a</think>\n\n正文\n\n<tool>b</tool>\n\n尾"
        assert _last_closed_proto_tag_end(md) == md.index("</tool>") + len("</tool>")

    def test_unclosed_not_counted(self):
        # 未闭合 <tool> 不产生「闭合标签结束」——它没有闭合标签
        md = "正文\n\n<tool>\nname: x"
        assert _last_closed_proto_tag_end(md) == -1


class TestComputeStreamStableLen:
    def test_plain_text_last_break(self):
        md = "段一\n\n段二"
        # 最后段落边界 + 2
        assert _compute_stream_stable_len(md) == md.index("\n\n") + 2

    def test_no_break_returns_zero(self):
        assert _compute_stream_stable_len("没有空行的单段") == 0

    def test_tool_result_blank_lines_must_not_split(self):
        """核心回归：工具 result 内部空行不得成为推进点（tail 不得带 </tool> 残片）。

        旧实现 _last_para_break_outside_fence 会把 result 里的 ``\n\n`` 当段落
        边界，stable 切进工具块中间。
        """
        body = "正文段落之前的内容\n\n"
        tool = "<tool>\nname: read_file\nargs: {}\nresult: import sys\n\nimport os\n\nx=1\nsuccess: True\n</tool>"
        tail = "\n\n后续正文段落"
        md = body + tool + tail
        stable = _compute_stream_stable_len(md)
        assert stable is not None
        tail_after = md[stable:]
        assert "</tool>" not in tail_after, f"tail 带协议残片：stable={stable} tail={tail_after[:80]!r}"
        assert "<tool>" not in tail_after

    def test_think_block_not_split(self):
        md = "<think>思考内容\n\n内含空行\n\n更多思考</think>\n\n正文段落"
        stable = _compute_stream_stable_len(md)
        assert "</think>" not in md[stable:]

    def test_multiple_tools_take_last(self):
        t1 = "<tool>a\n\nb</tool>"
        t2 = "<tool>c\n\nd</tool>"
        md = f"{t1}\n\n中段\n\n{t2}\n\n尾段"
        stable = _compute_stream_stable_len(md)
        assert stable >= md.index("</tool>", md.index(t2)) + len("</tool>")

    def test_unclosed_tool_blocks_advance(self):
        # 未闭合协议块：返回 0 = 不推进（与调用点原守卫语义一致）
        md = "正文\n\n<tool>\nname: x"
        assert _compute_stream_stable_len(md) == 0

    def test_unclosed_think_blocks_advance(self):
        md = "<think>\n思考中"
        assert _compute_stream_stable_len(md) == 0

    def test_fence_blank_lines_still_avoided(self):
        """原有语义保持：代码 fence 内部空行同样不得成为推进点。"""
        md = "段一\n\n```python\nx=1\n\ny=2\n```\n\n段二"
        stable = _compute_stream_stable_len(md)
        # 段二之前的边界在 fence 闭合之后
        assert stable == md.rindex("\n\n") + 2

    def test_tag_end_beats_earlier_break(self):
        """协议块闭合后无空行直接接正文：推进点取标签结束（>= 段界语义）。"""
        md = "段一\n\n<tool>x</tool>紧跟正文无空行"
        stable = _compute_stream_stable_len(md)
        assert stable >= md.index("</tool>") + len("</tool>")
        assert "</tool>" not in md[stable:]
