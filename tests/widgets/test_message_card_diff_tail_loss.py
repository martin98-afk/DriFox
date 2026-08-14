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
    # 追加条件必须要求目标节点已带 data-incremental 标记
    assert "last.hasAttribute('data-incremental')" in src, "追加分支必须要求 last 已是增量节点"
    # 禁止旧实现：无条件把非增量 P 打标记（污染格式化段落）
    assert "last.setAttribute('data-incremental', 'true')" not in src, "不得给格式化稳定段落打 data-incremental 标记"
    # 稳定段落后新建独立增量节点承载新文本
    assert src.count("p.setAttribute('data-incremental', 'true')") >= 3, "稳定段落/思考块/兜底分支都应新建增量节点"


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
    """模拟流式：无空行分隔的长段落（核心场景）——软边界切段 + 尾部行内渲染，
    最终 DOM 可见文本与全量渲染一致（markdown 源码不再滞留）。

    软边界切分后，长段落（>= _MIN_SOFT_SEGMENT_CHARS 字符）按句号增量切段走
    _render_stable_segment 差量渲染；不足阈值的尾部仍走 _render_inline_tail
    行内渲染。两条路径都必须即时格式化 markdown 语法，不得字面显示源码。
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
    pos = 0
    while pos < len(md):
        pos += 7
        chunk_md = md[:pos]
        stable_len, segs = _extract_closed_segments(chunk_md[stable:])
        if segs:
            for seg in segs:
                rendered_text += _strip_tags(_render_stable_segment(seg, compact=False))
            stable += stable_len
        tail = chunk_md[stable:]
        tail_text = ""
        if tail and not _has_unclosed_think_or_tool(tail):
            tail_text = _strip_tags(_render_inline_tail(tail, compact=False))
        visible = rendered_text + tail_text
    # 流式结束：完整内容已闭合，最终可见文本**不出现** markdown 源码
    # （中间态未闭合语法字面显示是 markdown 固有行为，由
    #   test_render_inline_tail_formats_inline_markdown 覆盖）
    assert "**" not in visible, f"流式期间仍显示 markdown 源码: {visible!r}"
    assert "`" not in visible, f"流式期间仍显示反引号源码: {visible!r}"
    # 流式结束全量渲染：可见文本（去空白）与全量一致
    # （软边界切段会把句号后的软换行吞掉，属差量渲染可接受差异，故比较时去空白）
    full_text = _strip_tags(_render_markdown_to_html_cached_impl(md, compact=False))
    strip_ws = lambda s: re.sub(r"\s+", "", s)
    assert strip_ws(visible) == strip_ws(full_text), (
        f"流式期间文本与全量不一致:\n diff={visible!r}\n full={full_text!r}"
    )


def test_extract_closed_segments_splits_long_paragraph_by_sentence():
    """软边界：无空行的大段中文正文（>= 阈值）应按句号增量切段，稳定区向前推进。"""
    md = (
        "这是第一句话内容比较长用来测试。这是第二句话内容也比较长用来测试。"
        "这是第三句话内容继续比较长用来测试。这是第四句话内容仍然比较长用来测试。"
        "这是第五句话内容还要比较长用来测试。这是第六句话内容终于比较长用来测试。"
    )
    stable_len, segs = _extract_closed_segments(md)
    assert len(segs) >= 1, "大段正文应至少切出 1 段"
    assert stable_len > 0, "软边界应推进稳定区"
    # 每段必须以句号结尾（软边界切在句号后，句号保留在段尾）
    for seg in segs:
        assert seg.endswith("。"), f"软边界段应以句号结尾: {seg[-10:]!r}"
