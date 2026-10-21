# -*- coding: utf-8 -*-
"""回归测试：插件注册 tag（<mood> 等）流式输出内容泄漏到正文（闪现后消失）。

现象（用户报告）
----------------
assistant_hub 人格块 <mood>/<plan> 流式输出期间，块内容先以普通正文
逐行出现；全量渲染落地后被替换为插件占位行（"解析中…"），闭合后才
变成完整卡片——视觉上"文字先流式出来，然后消失"。

根因（四条泄漏路径，think 已修过同款、tag 没有同等待遇）
--------------------------------------------------------
1. append_text 未闭合 tag 期间仍调 _append_text_incremental，生肉文本
   以纯文本进 DOM（<mood> 标签本身被当 HTML 吞掉，内容行可见）。
2. _has_unclosed_think_or_tool 不查注册 tag → tail 行内渲染 /
   _tail_before_unclosed_block / 差量基线守卫全部放行未闭合 tag。
3. _extract_closed_segments 段级守卫不查 tag：tag 含 \\n\\n 多段落时
   基线被推进到 tag 内部，闭合后 tail 无 open 有 close → 孤立 close
   被清理、内容当正文渲染。
4. _push_unrendered_tail_text 回补无守卫：快照切在 tag 中间时半截
   内容以纯文本回补 DOM。

修复
----
- 新增 _has_unclosed_registered_tag / _registered_tag_names_safe；
- _has_unclosed_think_or_tool 并入 tag 检测（一处覆盖所有守卫点）；
- _tail_before_unclosed_block 截断候选加入注册 tag 对；
- _extract_closed_segments 起点防护 + 段级守卫加入 tag；
- append_text 未闭合 tag 跳过增量注入，状态翻转（首现/闭合）强制
  全量渲染一次（占位行/完整卡片只能由全量管线产出）；
- _push_unrendered_tail_text 回补前守卫。
"""

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.message_card import (
    _extract_closed_segments,
    _has_unclosed_registered_tag,
    _has_unclosed_think_or_tool,
    _inject_tag_cards,
    _render_stable_segment,
    _tail_before_unclosed_block,
)


def noop_render(content: str, ctx: dict) -> str:
    return f"<div data-tag='{ctx.get('tag')}' data-completed='{ctx.get('completed')}'>x</div>"


def setup_function():
    UIPluginRegistry.get_instance().register_tag_renderer(
        plugin_name="t", tag_name="mood", render_func=noop_render, priority=10
    )


def teardown_function():
    UIPluginRegistry.get_instance().unload_plugin("t")


# ── 用例 1：未闭合注册 tag 检测 ──
def test_has_unclosed_registered_tag():
    assert not _has_unclosed_registered_tag(""), "空串应为 False"
    assert not _has_unclosed_registered_tag("正文"), "无标签应为 False"
    assert _has_unclosed_registered_tag("正文<mood>感受：x"), "未闭合 mood 应为 True"
    assert not _has_unclosed_registered_tag("正文<mood>感受：x</mood>后文"), "已闭合应为 False"
    assert _has_unclosed_registered_tag("<mood>a\n\nb</mood>ok<mood>c"), "最后一个未闭合应为 True"
    assert not _has_unclosed_registered_tag("<think>未闭合"), "think 不归 tag 检测管"


# ── 用例 2：_has_unclosed_think_or_tool 并入 tag 检测 ──
def test_unclosed_think_or_tool_includes_tag():
    assert _has_unclosed_think_or_tool("<mood>a"), "tag 未闭合应被视为未闭合协议块"
    assert not _has_unclosed_think_or_tool("<mood>a</mood>"), "tag 已闭合应为 False"


# ── 用例 3：tail 截断：未闭合 tag 之前正文保留，tag 段丢弃 ──
def test_tail_before_unclosed_block_cuts_at_tag_open():
    cut = _tail_before_unclosed_block("前文\n\n<mood>感受：x")
    assert cut == "前文\n\n", f"应截到未闭合 tag 起点: {cut!r}"


# ── 用例 4：差量切段：tag 未闭合不产出；起点在 tag 内部不产出；闭合正常产出 ──
def test_extract_closed_segments_tag_guard():
    stable, segs = _extract_closed_segments("<mood>感受：x\n\n联想：y</mood>\n\n正文")
    assert segs == [] and stable == 0, f"tag 未闭合期间不应产出闭合段: {(stable, segs)!r}"
    stable, segs = _extract_closed_segments("联想：y</mood>\n\n正文")
    assert segs == [] and stable == 0, f"起点落在 tag 内部不应产出: {(stable, segs)!r}"
    stable, segs = _extract_closed_segments("正文一\n\n<mood>x</mood>\n\n正文二")
    assert len(segs) == 2 and stable > 0, f"闭合 tag 应正常产出: {segs!r}"


# ── 用例 5：稳定段渲染：未闭合 tag 段 → 流式占位，不泄漏原文 ──
def test_render_stable_segment_unclosed_tag_renders_placeholder():
    html = _render_stable_segment("<mood>感受：x", compact=False)
    assert "data-completed='False'" in html, f"未闭合 tag 段应渲染流式占位: {html!r}"


# ── 用例 6：全量注入：闭合→完整卡；未闭合→占位；孤立 close 清理 ──
def test_inject_tag_cards_completed_vs_streaming():
    out = _inject_tag_cards("<mood>感受：x</mood>", True, compact=False)
    assert "data-completed='True'" in out, f"闭合 tag 应渲染完整卡: {out!r}"
    out = _inject_tag_cards("<mood>感受：x", True, compact=False)
    assert "data-completed='False'" in out, f"未闭合 tag 应渲染占位: {out!r}"
    out = _inject_tag_cards("联想：y</mood>", True, compact=False)
    assert "</mood>" not in out, f"孤立 close 应被清理: {out!r}"