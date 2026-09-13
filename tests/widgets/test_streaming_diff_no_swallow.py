# -*- coding: utf-8 -*-
"""回归测试：流式差量渲染不得吞内容（A/B/C 三处缺陷）。

现象（用户报告）
----------------
流式输出过程中，正文 / 正文区工具结果框偶尔吞内容：文字出现后消失且不恢复。

三处缺陷（均在 app/widgets/message_card.py）
--------------------------------------------
A. 差量渲染时 tail 含未闭合 `<think>` / `<tool>` → `_tail_html` 置空，
   而 JS `updateContentAppend` 无条件 remove 全部 `[data-incremental]` 节点
   → 未闭合块**之前**已显示的正文被一并吞掉（未闭合块内容本就该静默累积，
   但它之前的正文不该跟着消失）。

B. 全量渲染落地后 `_mark_unclosed_para_js` 只给末尾 `<p>` 打
   `data-incremental`。末尾是代码块/列表/引用时打不上 → 后续
   `updateTailHtml` / `updateContentAppend` 删不掉它，又把 tail 重建一遍
   → 同一段内容重复出现。

C. 全量渲染提交的是 **md 快照**；线程池在途期间到达的 chunk 只存在于 DOM
   增量节点，`updateContent` 整体替换 innerHTML → 这些文字被抹掉
   （视觉上"吞"，且要等下一个渲染周期才自愈）。
"""

import inspect

from app.widgets.message_card import (
    CodeWebViewer,
    _has_unclosed_think_or_tool,
    _tail_before_unclosed_block,
)


# ── A1：未闭合块之前的正文必须保留（不得被整段 tail 一起丢掉）──
def test_tail_before_unclosed_block_keeps_prose():
    """tail = 正文片段 + 未闭合 <tool> → 只截到未闭合块起点。"""
    assert _tail_before_unclosed_block("正文片段<tool>{}") == "正文片段"
    assert _tail_before_unclosed_block("正文片段<think>思考中") == "正文片段"


def test_tail_before_unclosed_block_keeps_closed_blocks():
    """已闭合块不截断（闭合后内容属于正常正文/卡片）。"""
    md = "正文<think>思考</think>尾部"
    assert _tail_before_unclosed_block(md) == md
    assert _has_unclosed_think_or_tool(md) is False


def test_tail_before_unclosed_block_passthrough_when_clean():
    """无未闭合块 / 空串 → 原样返回（不改变既有行为）。"""
    assert _tail_before_unclosed_block("") == ""
    assert _tail_before_unclosed_block("普通正文") == "普通正文"


def test_tail_before_unclosed_block_multi_para():
    """未闭合块之前的多个段落全部保留（不能只留最后一段）。"""
    md = '第一段。\n\n第二段<tool>{"a": 1}'
    assert _tail_before_unclosed_block(md) == "第一段。\n\n第二段"


# ── A2：差量分支必须接上该 helper（防回归）──
def test_diff_branch_uses_tail_before_unclosed_block():
    src = inspect.getsource(CodeWebViewer._perform_update)
    assert "_tail_before_unclosed_block" in src, "差量渲染 tail 必须截到未闭合块之前再渲染"


# ── B：全量渲染末尾打标必须覆盖代码块等块级元素（不只 <p>）──
def test_mark_unclosed_tail_js_covers_block_elements():
    src = inspect.getsource(CodeWebViewer._apply_render_result)
    assert "PRE" in src, "末尾是代码块（PRE / 代码包装 DIV）时也必须打 data-incremental，否则尾部重建会重复"


# ── C：渲染落地后必须补回「快照之后新增」的文本 ──
class _Page:
    def __init__(self):
        self.js = []

    def runJavaScript(self, js, cb=None):
        self.js.append(js)


class _Viewer:
    """最小 viewer 桩：只提供 _push_unrendered_tail_text 所需状态。"""

    _push_unrendered_tail_text = CodeWebViewer._push_unrendered_tail_text
    _append_text_incremental = CodeWebViewer._append_text_incremental

    def __init__(self, snapshot: str, latest: str):
        self._page = _Page()
        self._streaming = True
        self._is_js_ready = True
        self._markdown_text = latest
        self._last_rendered_markdown = snapshot
        self._current_adaptive_interval = 300
        self._last_chunk_time = 0.0

    def page(self):
        return self._page

    def isVisible(self):
        return True


def test_push_unrendered_tail_text_restores_new_chunks():
    """渲染快照之后到达的文本必须重新推回 DOM（updateContent 会抹掉它们）。"""
    v = _Viewer(snapshot="已渲染正文。", latest="已渲染正文。之后新到的文字")
    v._push_unrendered_tail_text()
    assert v._page.js, "新增文本必须被推回 DOM"
    assert "之后新到的文字" in v._page.js[-1]


def test_push_unrendered_tail_text_noop_when_nothing_new():
    """没有新增文本时不得重复推送（避免重复渲染）。"""
    v = _Viewer(snapshot="同样内容", latest="同样内容")
    v._push_unrendered_tail_text()
    assert v._page.js == []


def test_push_unrendered_tail_text_ignores_rewritten_content():
    """内容被整体替换（非追加）时不得推送（语义未知，交给下一次全量渲染）。"""
    v = _Viewer(snapshot="旧内容A", latest="完全不同的B")
    v._push_unrendered_tail_text()
    assert v._page.js == []


def test_apply_render_result_calls_push_unrendered_tail_text():
    src = inspect.getsource(CodeWebViewer._apply_render_result)
    assert "_push_unrendered_tail_text" in src, "全量渲染落地后必须补回快照之后的新增文本"


# ── B2：基线未推进时必须强制下一次全量渲染（否则差量把已渲染段再追加一遍）──
def test_unclosed_block_forces_full_render_next_round():
    src = inspect.getsource(CodeWebViewer._apply_render_result)
    assert "self._needs_full_render = self._streaming and _has_unclosed_think_or_tool" in src, (
        "md 含未闭合块时基线不推进，必须强制下一次全量渲染，否则已渲染的段会被差量重复追加"
    )
