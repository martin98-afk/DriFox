"""追问（<ask>）收拢渲染回归测试。

追问由模型分散输出，渲染时统一摘除、去重，在文末拼成「你可以继续问」区块。
这里锁住该行为：只影响卡片渲染，不改消息本体。
"""

import re

from app.widgets.message_card import _inject_context_links, _render_markdown_to_html_cached_impl


def test_asks_moved_to_tail_block():
    """正文里/末尾的 <ask> 全部摘除，只在文末保留一个区块。"""
    src = "先说结论。\n\n顺带一提，<ask>打包体积会涨吗</ask> 这点要注意。\n\n- <ask>行尾检查能关吗</ask>\n"
    out = _inject_context_links(src)
    assert out.count('<div class="ask-suggest">') == 1
    assert out.count('data-type="ask"') == 2
    # 区块必须是最后一段
    assert out.rstrip().endswith("</div>")
    assert out.index('<div class="ask-suggest">') > out.index("先说结论")
    # 正文里的标签与空列表项已清理
    assert "<ask>" not in out
    assert "这点要注意" in out


def test_asks_dedup_keep_order():
    """重复追问按内容去重，保留首次出现顺序。"""
    src = "- <ask>怎么回滚</ask>\n- <ask>会影响性能吗</ask>\n- <ask>怎么回滚</ask>\n"
    out = _inject_context_links(src)
    assert out.count('data-type="ask"') == 2
    assert out.index("怎么回滚") < out.index("会影响性能吗")


def test_empty_ask_tags_removed_without_block():
    """空 / 纯空白 ask 只摘除标签，不产出区块。"""
    src = "正文。\n\n- <ask></ask>\n- <ask>   </ask>\n"
    out = _inject_context_links(src)
    assert "<ask>" not in out
    assert "ask-suggest" not in out


def test_ask_inside_code_block_untouched():
    """代码块内的 <ask> 是示例文本，不算追问。"""
    src = "示例：\n\n```\n- <ask>代码里的假追问</ask>\n```\n\n- <ask>真追问</ask>\n"
    out = _inject_context_links(src)
    assert "代码里的假追问</ask>" in out  # 原样留在代码块里
    assert out.count('data-type="ask"') == 1
    assert "真追问" in out


def test_placeholder_ask_not_relocated_and_preserved():
    """模型照抄提示词模板输出的「原话/追问」不产出胶囊，但**原文必须保留**。

    旧行为是「删除且不展示」——正文里这两个词会凭空消失（用户可见丢字）。
    新不变量：不展示就不许删（宁可残留字面量）。
    """
    src = "- <ask>原话</ask>\n- <ask>追问</ask>\n- <ask>真的会变吗</ask>\n"
    out = _inject_context_links(src)
    assert 'data-type="ask"' in out
    assert out.count('data-type="ask"') == 1
    assert "真的会变吗" in out
    # 占位词原文保留（不丢字），只是不变成胶囊
    assert "原话" in out
    assert "追问" in out


def test_ask_count_capped():
    """超量追问只收拢前 4 条，其余**留在原文**（删了不展示 = 丢字）。"""
    src = "\n".join(f"- <ask>第{i}问</ask>" for i in range(1, 7))
    out = _inject_context_links(src)
    assert out.count('data-type="ask"') == 4
    # 第 5/6 条不再被静默删除
    assert "第5问" in out
    assert "第6问" in out
    assert "第1问" in out


def test_never_delete_unshown_ask():
    """不变量：凡从正文摘走的 <ask> 内容，必须出现在文末区块里。"""
    src = (
        "正文段落一。\n\n"
        "- <ask>原话</ask>\n"
        "- <ask>问题</ask>\n"
        "- <ask>第一条真追问</ask>\n"
        "- <ask>第二条真追问</ask>\n"
        "- <ask>第三条真追问</ask>\n"
        "- <ask>第四条真追问</ask>\n"
        "- <ask>第五条真追问</ask>\n"
        "\n尾部正文。\n"
    )
    out = _inject_context_links(src)
    block = out[out.index('<div class="ask-suggest">') :]
    body = out[: out.index('<div class="ask-suggest">')]
    block_keys = set(re.findall(r'data-content="([^"]*)"', block))
    # 正文里剩下的字面 <ask> 内容 + 区块内容 = 原文全部 ask 内容
    body_keys = set(re.findall(r"<ask>(.*?)</ask>", body, re.DOTALL))
    assert body_keys | block_keys == {
        "原话",
        "问题",
        "第一条真追问",
        "第二条真追问",
        "第三条真追问",
        "第四条真追问",
        "第五条真追问",
    }
    # 正文里没有任何内容被「删掉后不出现在任何地方」
    assert "尾部正文。" in body


def test_plain_text_unchanged():
    """没有追问时原文不动。"""
    src = "普通回复，没有追问。\n"
    assert _inject_context_links(src) == src


def test_block_survives_markdown_convert():
    """区块 HTML 要能穿过 md.convert 与后续管线（块级 HTML 不被吞）。"""
    src = "正文结束。\n\n- <ask>要改配置吗</ask>\n"
    html = _render_markdown_to_html_cached_impl(src)
    assert 'class="ask-suggest"' in html
    assert 'class="context-tag" data-type="ask"' in html


# ===== 误匹配熔断：宁可残留字面量，也不能把正文当成"追问内容"摘走 =====


def test_stray_open_tag_does_not_swallow_body():
    """正文里字面写出的孤立 <ask> 不得吞掉后续正文（内容凭空消失回归）。

    流式期闭合标签还没到，正文照常显示；一旦文末 </ask> 到达，非贪婪匹配会把
    两个标签之间的整段正文当成追问摘进胶囊 —— 表现为「渲染出来又全没了」。
    """
    src = "写法：把问题放进 <ask> 标签即可。\n\n第一段正文。\n\n第二段正文。\n\n<ask>要我继续吗</ask>\n"
    out = _inject_context_links(src)
    assert "第一段正文" in out
    assert "第二段正文" in out
    assert "标签即可" in out
    assert out.count('data-type="ask"') == 1
    assert "要我继续吗" in out


def test_ask_inside_think_block_untouched():
    """摘除跑在 think/tool 注入之前，块内的 <ask> 不参与收拢。"""
    src = "<think>记住用 <ask> 标签</think>\n\n正文。\n\n<ask>继续吗</ask>\n"
    out = _inject_context_links(src)
    assert "<think>记住用 <ask> 标签</think>" in out
    assert "正文。" in out
    assert out.count('data-type="ask"') == 1


def test_ask_inside_tool_block_untouched():
    """工具块内同理：块内 ask 留在块里，不搬到文末。"""
    src = "<tool>\nname: x\nresult: 用 <ask> 标签\n</tool>\n\n正文。\n\n<ask>继续吗</ask>\n"
    out = _inject_context_links(src)
    assert "result: 用 <ask> 标签" in out
    assert "正文。" in out
    assert out.count('data-type="ask"') == 1


def test_ask_inside_inline_code_untouched():
    """行内代码里的 <ask> 是示例，原样保留。"""
    src = "用法：`<ask>问题</ask>` 这样写。\n\n<ask>继续吗</ask>\n"
    out = _inject_context_links(src)
    assert "`<ask>问题</ask>`" in out
    assert out.count('data-type="ask"') == 1


def test_malformed_close_tag_not_an_ask():
    """闭合标签写错（</ ask>）时整段不是追问，原文不动。"""
    src = "说明：<ask>你的问题</ ask>\n\n正文。\n\n<ask>继续吗</ask>\n"
    out = _inject_context_links(src)
    assert "<ask>你的问题</ ask>" in out
    assert "正文。" in out
    assert out.count('data-type="ask"') == 1


# ===== 非正文区间保护：这些块里的 <ask> 是示例/卡片正文，摘走 = 丢字 =====


def test_ask_inside_indented_code_block_untouched():
    """缩进代码块（4 空格）里的 <ask> 是示例，必须原样保留。

    回归背景：_CODE_BLOCK_PATTERN 只认 ``` 围栏，缩进代码块不在保护区间内 →
    文档示例里的 <ask>问题</ask> 会被摘走且走「删除不展示」分支，纯丢字。
    """
    src = "示例：\n\n    <ask>问题</ask>\n\n正文。\n\n<ask>继续吗</ask>\n"
    out = _inject_context_links(src)
    assert "<ask>问题</ask>" in out
    assert "正文。" in out
    assert out.count('data-type="ask"') == 1
    assert "继续吗" in out


def test_ask_inside_indented_code_block_non_placeholder_untouched():
    """缩进代码块里的非占位词 <ask> 也不得被搬进文末胶囊。"""
    src = "配置示例：\n\n    <ask>要改成什么格式</ask>\n"
    out = _inject_context_links(src)
    assert "<ask>要改成什么格式</ask>" in out
    assert "ask-suggest" not in out


def test_ask_inside_plan_block_untouched():
    """plan 块由 _inject_tag_cards 渲染成卡片，块内 <ask> 属卡片正文 → 不摘。"""
    src = "<plan>\n目标：x\n追问：<ask>想接着做哪块？</ask>\n</plan>\n\n正文。\n"
    out = _inject_context_links(src)
    assert "<ask>想接着做哪块？</ask>" in out
    assert "ask-suggest" not in out


def test_ask_inside_mood_block_untouched():
    """mood 等人格块同理。"""
    src = "<mood>\n<ask>要接着聊吗？</ask>\n</mood>\n\n正文。\n"
    out = _inject_context_links(src)
    assert "<ask>要接着聊吗？</ask>" in out
    assert "ask-suggest" not in out


def test_ask_inside_system_reminder_untouched():
    """hook 提示词模板示例常出现在 system-reminder 里 → 不得变成用户可见胶囊。"""
    src = "<system-reminder>\n格式：\n- <ask>追问内容</ask>\n</system-reminder>\n\n正文。\n\n<ask>继续吗</ask>\n"
    out = _inject_context_links(src)
    assert "<ask>追问内容</ask>" in out
    assert out.count('data-type="ask"') == 1
    assert "继续吗" in out


def test_ask_inside_hook_block_untouched():
    """<hook event="X"> 块（旧格式 hook 注入）同理。"""
    src = '<hook event="Stop">\n- <ask>原话</ask>\n</hook>\n\n正文。\n\n<ask>继续吗</ask>\n'
    out = _inject_context_links(src)
    assert "<ask>原话</ask>" in out
    assert out.count('data-type="ask"') == 1


def test_ask_inside_unclosed_fence_untouched():
    """未闭合代码块（流式中间态）里的 <ask> 不得被摘走。"""
    src = "示例：\n```html\n<div><ask>问题</ask></div>\n"
    out = _inject_context_links(src)
    assert "<ask>问题</ask>" in out
    assert "ask-suggest" not in out


def test_real_followups_still_collected_among_examples():
    """混合场景：代码示例原样保留，真追问照常收拢到文末。"""
    src = (
        "格式说明：\n\n"
        "    <ask>示例内容</ask>\n\n"
        "```\n<ask>代码里的示例</ask>\n```\n\n"
        "行内示例 `<ask>行内</ask>` 就这些。\n\n"
        "正文结束。\n\n"
        "- <ask>真追问一</ask>\n"
        "- <ask>真追问二</ask>\n"
    )
    out = _inject_context_links(src)
    assert out.count('data-type="ask"') == 2
    assert "示例内容" in out
    assert "代码里的示例" in out
    assert "行内" in out
    assert out.index("真追问一") < out.index("真追问二")
