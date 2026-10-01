"""追问（<ask>）收拢渲染回归测试。

追问由模型分散输出，渲染时统一摘除、去重，在文末拼成「你可以继续问」区块。
这里锁住该行为：只影响卡片渲染，不改消息本体。
"""

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


def test_placeholder_ask_dropped():
    """模型照抄提示词模板输出的「原话」等占位词不是真追问。"""
    src = "- <ask>原话</ask>\n- <ask>追问</ask>\n- <ask>真的会变吗</ask>\n"
    out = _inject_context_links(src)
    assert "原话" not in out
    assert 'data-type="ask"' in out
    assert out.count('data-type="ask"') == 1
    assert "真的会变吗" in out


def test_ask_count_capped():
    """超量追问截断，避免卡片尾部过长（上限 4）。"""
    src = "\n".join(f"- <ask>第{i}问</ask>" for i in range(1, 7))
    out = _inject_context_links(src)
    assert out.count('data-type="ask"') == 4
    assert "第5问" not in out


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
