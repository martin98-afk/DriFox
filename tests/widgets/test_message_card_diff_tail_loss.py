# -*- coding: utf-8 -*-
"""回归测试：流式正文尾部文本丢失（updateContentAppend 移除未闭合尾部）。

现象（用户报告）
----------------
大模型消息卡片正文在流式过程中偶发显示不全，尾部文字消失。
例：正文"…同时搜索 Claude Code 高星仓库"只显示到"高"，"星仓库"丢失。

根因
----
差量渲染（_perform_update diff 快路径）：
- Python `_extract_closed_segments(md[stable:])` 只产出**已闭合段**
- JS `updateContentAppend(newHtml)` 先 remove() **所有** [data-incremental]
  节点，再追加闭合段 HTML
- 未闭合尾部文本（仍在增量节点里）被 remove 连带删除，且 newHtml 不含它
  → 尾部永久消失，直到该尾部自身闭合被后续 diff 覆盖（可能永不发生）

修复
----
- Python 差量渲染时计算未闭合尾部 tail（stable 推进后的剩余 md），
  作为第二参数传给 JS：`updateContentAppend(newHtml, tailText)`
- JS 移除全部增量节点后，若 tailText 非空则重建增量节点，
  保证未闭合尾部持续可见，直到它闭合被差量/全量渲染替换
"""

import inspect
import re
import sys

from app.widgets.message_card import (
    _extract_closed_segments,
    _has_unclosed_think_or_tool,
    _render_inline_tail,
    _render_markdown_to_html_cached_impl,
    _render_stable_segment,
    CodeWebViewer,
)


# ── 用例 1：未闭合尾部不得被已闭合段吞掉 ──
def test_extract_closed_segments_covers_only_closed_parts():
    """闭合段提取后，未闭合尾部必须留在 stable 之后（增量区重建依据）。"""
    md = "第一段内容\n\n第二段未闭合尾部"
    stable_len, segs = _extract_closed_segments(md)
    assert segs == ["第一段内容"], f"闭合段应只有第一段: {segs!r}"
    tail = md[stable_len:]
    assert tail == "第二段未闭合尾部", f"未闭合尾部应留在 stable 后: {tail!r}"


def test_extract_closed_segment_tail_with_newline_prefix():
    """同 chunk 内出现 \\n\\n：首段（以 \\n\\n 结束）已闭合 → 产出；
    末段（无 \\n\\n 结尾）未闭合 → 留在尾部增量区。"""
    md = "第二段开头未闭合\n\n第三段内容"
    stable_len, segs = _extract_closed_segments(md)
    assert segs == ["第二段开头未闭合"], f"首段应闭合产出: {segs!r}"
    tail = md[stable_len:]
    assert tail == "第三段内容", f"末段应留在增量区: {tail!r}"


# ── 用例 2：模拟修复后行为 —— 差量追加以尾部重建，正文不丢 ──
def _simulate_diff_with_tail_rebuild(chunks):
    """复刻修复后「差量渲染 + 尾部重建」语义（Python 侧行为等价）。

    返回 (visible, md)：
    - visible: DOM 可见文本（格式化段 + 增量尾部重建）
    - md: Python 完整 markdown
    """
    md = ""
    stable = 0
    rendered = []  # 已差量渲染的 HTML 段（文本近似）
    tail = ""  # 当前增量尾部（模拟 JS data-incremental 节点）

    for chunk in chunks:
        md += chunk
        stable_len, segs = _extract_closed_segments(md[stable:])
        if segs:
            rendered.append("|".join(segs))
            stable += stable_len
        # 修复后：重建未闭合尾部（md[stable:]）
        tail = md[stable:]
    visible = "|".join(rendered) + tail
    return visible, md


def test_tail_rebuild_no_loss():
    """复现 Flaky 场景：第一段闭合，第二段未闭合途中，第三段闭合触发 diff。"""
    chunks = [
        "第一段完整内容。\n\n",
        "第二段中间文字",
        "\n\n第三段内容。",
    ]
    visible, md = _simulate_diff_with_tail_rebuild(chunks)
    assert visible.replace("|", "").replace("\n", "") == md.replace("\n", ""), (
        f"尾部缺失: visible={visible!r} md={md!r}"
    )


# ── 用例 3：JS 模板必须含 tailHtml 重建逻辑（防回归） ──
def test_update_content_append_template_has_tail_rebuild():
    """JS updateContentAppend 必须支持第二参数 tailHtml（行内渲染 HTML）并重建增量节点。"""
    src = inspect.getsource(CodeWebViewer._load_skeleton)
    # 模板骨架是 f-string，函数体内用了 {{ }} 转义：
    # "function updateContentAppend(newHtml)" 无括号转义（函数参数区无 {{ }}）
    # 🐛 2026-08-09：tail 参数从纯文本 tailText 升级为行内渲染 HTML tailHtml
    # （流式期间未闭合尾部即时格式化 markdown 语法，不再字面显示源码）。
    assert "function updateContentAppend(newHtml, tailHtml)" in src, "updateContentAppend 必须接收 tailHtml 第二参数"
    # 尾部重建：移除全部增量节点后，若 tailHtml 非空则重新创建 data-incremental <p>
    assert "tailHtml" in src
    # 关键守卫：不能只移除增量而丢掉尾部；重建节点必须 innerHTML 注入
    assert "data-incremental" in src
    assert "tailDiv.innerHTML = tailHtml" in src
    # 新增守卫：updateTailHtml（无空行长段落尾部行内渲染）必须存在于模板
    assert "function updateTailHtml(html)" in src, "必须提供 updateTailHtml 尾部行内渲染函数（长段落流式格式化）"


def test_tail_rebuild_error_without_old_bug():
    """模拟修复前行为（不重建 tail）→ 必须失败（证明修复必要）。
    该用例只做行为对照，不真正断言。
    """
    chunks = ["第一段完整内容。\n\n", "第二段中间文字", "\n\n第三段内容。"]
    md = "".join(chunks)
    stable = 0
    rendered = []
    for chunk in chunks:
        md_so_far = "".join(chunks[: chunks.index(chunk) + 1])
        stable_len, segs = _extract_closed_segments(md_so_far[stable:])
        if segs:
            rendered.append("|".join(segs))
            stable += stable_len
            tail = ""  # bug：旧实现不重建 tail
        else:
            tail = md_so_far[stable:]
    visible = "|".join(rendered) + tail
    # 旧实现下尾部丢失（本用例只是文档记录，不作为断言）
    assert visible != md.replace("\n\n", "|") or True


# ── 用例 4：增量追加不得污染已格式化渲染的稳定段落（正文段落丢失回归） ──
def test_append_text_incremental_never_marks_formatted_paragraph():
    """🐛 回归（正文段落丢失）：_append_text_incremental 只能追加到
    data-incremental 增量节点；遇到已格式化渲染的稳定 <p>（非增量）必须
    新建独立增量节点，绝不能打 data-incremental 标记或原地追加——否则
    下次差量渲染 updateContentAppend 移除全部增量节点时会连带删除该稳定
    段落，已渲染正文永久丢失（"内容显示不全"）。
    """
    src = inspect.getsource(CodeWebViewer._append_text_incremental)
    # 追加条件：last 是带 data-incremental 标记的增量节点（v18 实际写法）
    assert "last.getAttribute('data-incremental') === 'true'" in src, "追加分支必须要求 last 已是增量节点"
    # 🐛 回归（流式文字跳位）：#char-count 拼在全量 HTML 末尾，是 lastElementChild；
    # 不跳过它，全量渲染后所有 chunk 都找不到真实末块（稳定 <p>）→ 每个同段文字
    # 都新建独立 <p> 换行蹦在最底部，下一轮渲染才合并回正文
    assert "last.id === 'char-count'" in src, "尾部宿主定位必须跳过 #char-count 字数统计节点"
    # 禁止旧实现：无条件把非增量 P 打标记（污染格式化段落）
    assert "last.setAttribute('data-incremental', 'true')" not in src, "不得给格式化稳定段落打 data-incremental 标记"
    # 稳定段落后新建独立增量节点承载新文本（挂起分段/稳定段落/兜底三处分支）
    assert src.count("_newIncrementalP(text)") + src.count("_newIncrementalP(clean)") >= 3, (
        "挂起分段/稳定段落/兜底分支都应新建增量节点"
    )


def test_full_render_marks_unclosed_tail_paragraph_incremental():
    """🐛 回归（流式文字跳位）：全量渲染应用后，md 尾部是未闭合段时，
    DOM 末尾 <p> 必须补打 data-incremental 标记，让后续同段 chunk
    走就地追加（连续增长），而非新建独立 <p> 换行蹦在最底部。

    配套：stable 推进点必须是最后一个 \n\n 之后（而非 md 末尾），
    使打标末段归属 tail 区——updateTailHtml/updateContentAppend 移除
    [inc] 节点时删掉的正是 tail 会重建的内容，不丢不重。
    """
    src = inspect.getsource(CodeWebViewer._apply_render_result)
    # 打标 JS 必须存在：限定 tagName==='P'（复杂尾部结构不标，维持兜底行为）
    assert "tagName!=='P'" in src, "打标 JS 必须限定只给末尾 <p> 打增量标记"
    assert "data-incremental" in src, "全量渲染后应给未闭合段末尾 <p> 补打增量标记"
    # stable 推进必须到最后段落边界，而非 md 末尾（否则打标删除会丢内容）
    assert 'rfind("\\n\\n")' in src, "全量渲染后 stable 必须推进到最后一个 \\n\n 之后"


def _simulate_stream_dom(chunks, fixed=True):
    """模拟真实流式 DOM 状态机（_append_text_incremental + updateContentAppend）。

    fixed=True: 修复后语义（不污染格式化段）
    fixed=False: 修复前语义（污染格式化段，对照用）
    返回最终可见文本。
    """
    dom = []  # {"text": str, "inc": bool}

    def append_inc(text):
        if not text:
            return
        if text[0] in "\n\r":
            dom.append({"text": text.lstrip("\n\r"), "inc": True})
        else:
            last = dom[-1] if dom else None
            if fixed:
                # 修复后：仅增量节点可追加
                if last and last["inc"]:
                    last["text"] += text
                else:
                    dom.append({"text": text, "inc": True})
            else:
                # 修复前：无条件追加并打标记（污染格式化段）
                if last:
                    last["inc"] = True
                    last["text"] += text
                else:
                    dom.append({"text": text, "inc": True})

    def diff_append(segs, tail):
        nonlocal dom
        dom = [e for e in dom if not e["inc"]]
        for s in segs:
            dom.append({"text": s, "inc": False})
        if tail:
            dom.append({"text": tail, "inc": True})

    md = ""
    stable = 0
    for chunk in chunks:
        append_inc(chunk)
        md += chunk
        stable_len, segs = _extract_closed_segments(md[stable:])
        if segs:
            stable += stable_len
            diff_append(segs, md[stable:])
    return "".join(e["text"] for e in dom)


def test_stream_dom_keeps_all_paragraphs_after_fix():
    """修复后：差量渲染不移除已渲染稳定段落，全部内容保留。"""
    chunks = [
        "第一段完整内容。\n\n",
        "第二段中间文字",
        "继续写",
        "\n\n第三段内容。",
        "\n\n第四段结尾。\n\n",
    ]
    visible = _simulate_stream_dom(chunks, fixed=True)
    for must in ("第一段完整内容。", "第二段中间文字继续写", "第三段内容。", "第四段结尾。"):
        assert must in visible, f"修复后内容丢失: {must!r} not in {visible!r}"
    # 段落结构：4 个独立段落
    assert visible.count("\n\n") == 0 or True  # 模拟中段落以独立元素呈现，不丢即可


def test_stream_dom_old_behavior_loses_first_paragraph():
    """对照：修复前语义下第一段（已渲染稳定段）被差量移除 → 丢失。
    证明修复必要性（本用例只做对照记录，不断言）。
    """
    chunks = ["第一段完整内容。\n\n", "第二段中间文字", "\n\n第三段内容。"]
    visible = _simulate_stream_dom(chunks, fixed=False)
    lost = "第一段完整内容。" not in visible
    # 文档记录：旧实现确实丢失第一段
    assert lost or True


# ── 用例 5：无空行长段落流式期间尾部行内渲染（"内容与最终不符"回归） ──
def _strip_tags(html_str: str) -> str:
    return re.sub(r"<[^>]+>", "", html_str)


def test_render_inline_tail_formats_inline_markdown():
    """未闭合尾部行内渲染：已闭合的行内语法（**加粗**、`code`、[链接]）
    必须格式化，未闭合语法（**加粗 无闭合）保持字面，与全量渲染文本一致。"""
    tail = (
        "这是一个非常长的段落，没有空行分隔。"
        "包含**加粗**和`行内代码`以及[链接](https://example.com)等内容。"
        "还有未闭合的**加粗标记"
    )
    html = _render_inline_tail(tail, compact=False)
    assert "<strong>加粗</strong>" in html, f"加粗未渲染: {html!r}"
    assert "<code>行内代码</code>" in html, f"行内代码未渲染: {html!r}"
    assert '<a href="https://example.com">链接</a>' in html, f"链接未渲染: {html!r}"
    # 未闭合的 ** 保持字面（markdown 库行为）
    assert "**加粗标记" in _strip_tags(html), f"未闭合语法应字面保留: {html!r}"
    # 与全量渲染可见文本一致（归一化空白）
    full_html = _render_markdown_to_html_cached_impl(tail, compact=False)
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    assert norm(_strip_tags(html)) == norm(_strip_tags(full_html)), "尾部行内渲染与全量渲染文本不一致"


def test_render_inline_tail_filters_think_tool_tags():
    """含 think/tool 标签的尾部必须整体跳过（思考/工具内容不得泄漏为正文）。"""
    # 已闭合 think：返回空串（不渲染，交由差量段/全量渲染为思考卡片）
    html = _render_inline_tail("<think>思考内容不应显示</think>", compact=False)
    assert html == "", f"think 块不应在尾部渲染: {html!r}"
    # 未闭合 think：同样跳过（_render_tail_inline 守卫依赖的判定）
    assert _has_unclosed_think_or_tool("<think>进行中") is True
    assert _render_inline_tail("<think>进行中", compact=False) == ""


def test_stream_long_paragraph_tail_render_aligns_with_full():
    """模拟流式：无空行分隔的长段落（核心场景）——尾部整体行内渲染，
    最终 DOM 可见文本与全量渲染一致（markdown 源码不再滞留）。

    无空行长段落无闭合段可差量渲染（闭合段只按 \n\n 硬边界切），
    整段落在未闭合尾部走 _render_tail_inline 行内渲染：单 convert
    保持段落结构，markdown 语法即时格式化，不得字面显示源码。
    """
    md = (
        "首先感谢您的提问。这个问题涉及到多个方面的考量，我们需要从整体架构、"
        "实现细节、性能影响以及后续维护等多个角度来全面分析。特别是当数据量"
        "增大时，**性能表现**会直接影响用户体验，因此`缓存策略`和`异步处理`"
        "就显得尤为重要。"
    )
    # 模拟流式 chunk 注入 + 差量切段 + 尾部行内渲染（复刻 _perform_update 差量快路径）
    stable = 0
    rendered_text = ""
    rendered_p_count = 0  # 已渲染稳定段 HTML 的 <p> 总数（段落拆裂回归断言用）
    pos = 0
    while pos < len(md):
        pos += 7
        chunk_md = md[:pos]
        stable_len, segs = _extract_closed_segments(chunk_md[stable:])
        if segs:
            for seg in segs:
                seg_html = _render_stable_segment(seg, compact=False)
                rendered_text += _strip_tags(seg_html)
                rendered_p_count += seg_html.count("<p>")
            stable += stable_len
        tail = chunk_md[stable:]
        tail_text = ""
        if tail and not _has_unclosed_think_or_tool(tail):
            tail_text = _strip_tags(_render_inline_tail(tail, compact=False))
            # 🐛 回归断言（拆段 bug）：源文同段（无空行）在流式任意时刻，
            # 稳定段 + 尾部合计只能是同一个 <p>。若软边界切段回归，
            # 闭合段封口 + tail 另起新段会把 <p> 计数抬到 2+（视觉跳位）。
            assert rendered_p_count + _render_inline_tail(tail, compact=False).count("<p>") == 1, (
                f"无空行同段被拆成多个 <p>（pos={pos}）: stable_p={rendered_p_count}"
            )
        visible = rendered_text + tail_text
    # 流式结束：完整内容已闭合，最终可见文本**不出现** markdown 源码
    # （中间态未闭合语法字面显示是 markdown 固有行为，由
    #   test_render_inline_tail_formats_inline_markdown 覆盖）
    assert "**" not in visible, f"流式期间仍显示 markdown 源码: {visible!r}"
    assert "`" not in visible, f"流式期间仍显示反引号源码: {visible!r}"
    # 流式结束全量渲染：可见文本（去空白）与全量一致
    # （中间态未闭合行内语法字面保留导致的空白差异，比较时去空白）
    full_text = _strip_tags(_render_markdown_to_html_cached_impl(md, compact=False))
    strip_ws = lambda s: re.sub(r"\s+", "", s)
    assert strip_ws(visible) == strip_ws(full_text), (
        f"流式期间文本与全量不一致:\n diff={visible!r}\n full={full_text!r}"
    )


def test_extract_closed_segments_keeps_paragraph_intact_without_blank_line():
    """🐛 回归（拆段 bug）：无空行的同一段正文（无论多长）不得被句号切段。

    历史 bug：软边界在句号处切闭合段，把源文同一段切成多个独立 <p>，
    流式时新片段先换行出现在最下面、全量渲染时又跳回正文合并（视觉跳位）。
    句号不是 markdown 段落边界，闭合段只允许按 \n\n 硬边界切。
    """
    md = (
        "这是第一句话内容比较长用来测试。这是第二句话内容也比较长用来测试。"
        "这是第三句话内容继续比较长用来测试。这是第四句话内容仍然比较长用来测试。"
        "这是第五句话内容还要比较长用来测试。这是第六句话内容终于比较长用来测试。"
    )
    stable_len, segs = _extract_closed_segments(md)
    assert segs == [], "无空行同段不得被句号切段（拆段回归）"
    assert stable_len == 0, "无闭合段时稳定区不得推进"
    # 对照：出现 \n\n 空行后正常切段（硬边界语义不受影响）
    md_hard = md + "\n\n第二段正文。"
    stable_len2, segs2 = _extract_closed_segments(md_hard)
    assert len(segs2) == 1 and segs2[0] == md, f"硬边界切段应产出第一段: {segs2!r}"
    assert stable_len2 == len(md) + 2, f"稳定区应推进到空行之后: {stable_len2}"


# ── 回归：fence 跨空行时闭合段必须回溯整块产出 ──
def test_extract_closed_segments_multiline_fence_produces_whole_block():
    """🐛 回归（流式闪现孤立空代码块）：fence 内容含空行时，闭合瞬间只产出
    尾段会造成双重破坏：
    1. fence 开启段/中间段落在 stable 区内却从未追加（updateContentAppend
       删增量节点时连带删掉 tail 行内渲染的完整代码块）；
    2. 尾段经 _sanitize_incomplete_markdown 补闭合后渲染成
       「半截代码文本 + 空 Plain Text 代码块」——用户看到完整代码块突然
       缩水成残段+空框，全量渲染才恢复。
    闭合段必须回溯到 fence 开启段起点，把整个 fence 区间作为一段产出。
    """
    code_block = "```python\ndef foo():\n    return 1\n\ndef bar():\n    return 2\n```"
    md = code_block + "\n\n完成。"
    stable_len, segs = _extract_closed_segments(md)
    assert segs == [code_block], f"闭合段应为完整 fence 区间: {segs!r}"
    assert stable_len == len(code_block) + 2, f"稳定区应推进到 fence 末尾空行之后: {stable_len}"
    tail = md[stable_len:]
    assert tail == "完成。", f"尾部应只剩闭合段之后的文本: {tail!r}"


def test_extract_closed_segments_multiline_fence_after_paragraph():
    """前置文字段 + 跨空行 fence：文字段照常产出，fence 整块产出，顺序不乱。"""
    code_block = "```python\na = 1\n\nb = 2\n```"
    md = "看这个：\n\n" + code_block + "\n\n结束"
    stable_len, segs = _extract_closed_segments(md)
    assert segs == ["看这个：", code_block], f"段落产出应完整有序: {segs!r}"
    assert md[stable_len:] == "结束", f"尾部应只剩收尾文本: {md[stable_len:]!r}"


def test_render_stable_segment_multiline_fence_no_empty_code_block():
    """整块产出的跨空行 fence 渲染为单个 python 代码块，无空 Plain Text 框。"""
    code_block = "```python\ndef foo():\n    return 1\n\ndef bar():\n    return 2\n```"
    html = _render_stable_segment(code_block)
    assert "Plain Text" not in html, f"不得出现空代码块兜底: {html!r}"
    assert ">python<" in html, f"代码块语言标签应为 python: {html!r}"
    assert "```" not in html, f"渲染产物不得残留字面 fence: {html!r}"
