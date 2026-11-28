# -*- coding: utf-8 -*-
"""思考摘要小节标题的解析与渲染回归。

背景：Responses API 的 reasoning summary 没有独立 title 字段，分段标题就是
summary_text 首行的 `**标题**`（ChatGPT / Codex 客户端同款）。渲染层把该行
提到折叠框 summary 行，正文里去掉，避免展开后重复一行带星号的标题。
无标题形态（chat/completions 的 reasoning_content）必须保持旧行为。
"""

from app.widgets.card_render_core import _get_think_preview, _render_think_block, _split_think_title


def test_split_title_extracts_bold_first_line():
    title, body = _split_think_title("**Using the todo tool**\n\nI think I need to call it.")
    assert title == "Using the todo tool"
    assert body == "I think I need to call it."


def test_split_title_no_title_keeps_content():
    raw = "核对一下状态：三个文件都是 staged。\n\n无待办。"
    title, body = _split_think_title(raw)
    assert title == ""
    assert body == raw


def test_split_title_only_heading():
    """只有标题行（无正文）时正文为空，调用方回退原内容"""
    title, body = _split_think_title("**Plan**")
    assert title == "Plan"
    assert body == ""


def test_split_title_ignores_inline_bold():
    """标题必须是独占首行；行内加粗不算标题"""
    raw = "先看 **重点** 再动手。\n第二行"
    title, body = _split_think_title(raw)
    assert title == ""
    assert body == raw


def test_render_put_title_in_summary_and_strip_from_body():
    html = _render_think_block("**Using the todo tool**\n\n正文内容在这里。", completed=True)
    # 标题进 summary 行
    assert "Using the todo tool" in html
    # 正文里不再出现带星号的标题行
    assert "**Using the todo tool**" not in html
    assert "正文内容在这里。" in html


def test_render_without_title_keeps_preview_behavior():
    html = _render_think_block("核对一下状态：三个文件都是 staged。", completed=True)
    assert "核对一下状态" in html
    assert "**" not in html

def test_render_compact_with_title_shows_title_plus_body_sample():
    """简洁模式单行（不可展开）：带标题特例必须「标题 · 正文片段」拼接。

    回归背景：088c365e 把预览统一换成标题后，简洁模式思考单行只剩 5 词标题，
    正文（单行形态唯一可见内容）彻底不可见——用户看到「标题有、内容看不到」。
    折叠框形态正文在展开区完整可见，不受影响。
    """
    content = "**Planning fresh news search**\n\nI see that the search results have been unreliable. So, I want to query fresh sources like Reuters."
    html = _render_think_block(content, completed=True, compact=True)
    assert "Planning fresh news search" in html, f"标题丢失: {html!r}"
    assert "So, I want to query fresh sources like Reuters." in html, f"正文片段丢失（单行只剩标题）: {html!r}"
    assert "**" not in html, f"星号标题行泄漏: {html!r}"

def test_render_compact_without_title_shows_first_statement():
    """无标题形态（chat/completions reasoning_content）：无结论标记时展示首个陈述。"""
    content = "核对一下状态：三个文件都是 staged。继续处理剩余改动。"
    html = _render_think_block(content, completed=True, compact=True)
    assert "核对一下状态" in html, f"预览采样丢失: {html!r}"

def test_render_compact_title_only_no_separator_tail():
    """只有标题行（无正文）：单行只显示标题，不带「 · 」空尾巴。"""
    html = _render_think_block("**Plan**", completed=True, compact=True)
    assert "Plan" in html
    assert " · " not in html, f"空正文拼出悬空分隔符: {html!r}"


def test_preview_prefers_confirmed_finding_over_early_suggestion_and_followup():
    content = "所以先查缓存。已确认根因是连接池耗尽。下一步补回归测试。"
    assert _get_think_preview(content) == "已确认根因是连接池耗尽。"


def test_preview_keeps_negation_and_ignores_later_hypothesis():
    content = "先看日志。最终不建议清空缓存。如果根因是网络，还需要再做验证。"
    assert _get_think_preview(content) == "最终不建议清空缓存。"


def test_preview_prefers_latest_finding_and_preserves_technical_names():
    content = "The root cause may be DNS. Confirmed that api.py still loads v1.2. Next, add a test."
    assert _get_think_preview(content) == "Confirmed that api.py still loads v1.2."


def test_preview_skips_code_when_prose_is_available():
    content = "先看日志。\n下一步检查连接池。\n```python\nreason = '根因是超时'\n```"
    assert _get_think_preview(content) == "下一步检查连接池。"


def test_preview_fallback_and_truncation():
    assert _get_think_preview("让我看看。\n请求在第二次重试时超时。") == "请求在第二次重试时超时。"
    assert _get_think_preview("一段很长的连续文本" * 20, max_length=8) == "一段很长的..."
    assert _get_think_preview(" \n ") == ""
