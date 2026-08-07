# -*- coding: utf-8 -*-
"""测试 _render_diff_preview 单列/双列双视图的词级高亮输出一致性。

目的（防御性回归，为候选优化 #1 铺路）：
_render_diff_preview 在单列分支（.diff-seg-col）先构建 paired_htmls 缓存
（一次 _highlighted_word_diff_html 调用），随后双列分支（.diff-seg-paired）
对同一组配对行又独立调用了一次 _highlighted_word_diff_html。两者输入完全相同，
因此当前实现下输出必须字节级一致。后续若把双列分支改为复用 paired_htmls 缓存，
测试应保持全绿；若错误复用导致任一视图输出变化，本测试立即失败。

覆盖场景：
1. pair=1 单行 + 词级差异命中（SequenceMatcher replace）
2. pair=5 多行 + 词级差异命中
3. 无词级差异（del/add 文本 token 序列相同 → SequenceMatcher 全 equal，无 word span）
4. 含未配对行（del/add 数量不等，配对部分仍必须一致）
5. >2000 字符 fast-path（跳过 SequenceMatcher，纯语法高亮）
6. 多个差异段（多文件/多 hunk）逐段一致
"""

import re

from app.widgets.render_helpers import _render_diff_preview

# 单行 cell 的正则（code 内容为 span 内联，非贪婪匹配到首个 </span></div> 安全闭合）
_CELL_DEL_RE = re.compile(r'<div class="diff-line diff-del">.*?<span class="line-code">(.*?)</span></div>', re.S)
_CELL_ADD_RE = re.compile(r'<div class="diff-line diff-add">.*?<span class="line-code">(.*?)</span></div>', re.S)


def _matching_close(html: str, from_pos: int) -> int:
    """从 from_pos 开始做 div 嵌套深度计数，返回自闭合容器的结束位置（含 </div> 之后）。"""
    depth = 1
    pos = from_pos
    while depth > 0:
        next_open = html.find("<div", pos)
        next_close = html.find("</div>", pos)
        if next_close == -1:
            raise AssertionError("未找到匹配的 </div>")
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + 4
        else:
            depth -= 1
            pos = next_close + 6
    return pos


def _extract_container_all(html: str, cls: str) -> list:
    """返回 html 中所有 `<div class="cls">` 容器的完整片段（按出现顺序）。"""
    out = []
    marker = f'<div class="{cls}">'
    pos = 0
    while True:
        start = html.find(marker, pos)
        if start == -1:
            break
        end = _matching_close(html, start + len(marker))
        out.append(html[start:end])
        pos = end
    return out


def _col_pair_codes(seg_col_html: str) -> list:
    """单列视图：按顺序提取所有 del/add cell 的 code，配成 (old, new) 对。

    单列布局为"所有 del 先、所有 add 后"，配对顺序与进入 paired_htmls
    的顺序一致：第 k 个 del cell ↔ 第 k 个 add cell。zip 天然截取前 pair 个。
    """
    dels = _CELL_DEL_RE.findall(seg_col_html)
    adds = _CELL_ADD_RE.findall(seg_col_html)
    return list(zip(dels, adds))


def _paired_pair_codes(seg_paired_html: str) -> list:
    """双列视图：遍历每个 .diff-seg-row，提取 (del code, add code) 对。"""
    pairs = []
    for row in _extract_container_all(seg_paired_html, "diff-seg-row"):
        del_m = _CELL_DEL_RE.search(row)
        add_m = _CELL_ADD_RE.search(row)
        if del_m and add_m:  # 跳过空占位行（unpaired 的 empty cell 无 diff-del/add class）
            pairs.append((del_m.group(1), add_m.group(1)))
    return pairs


def _assert_dual_view_consistency(diff_text: str):
    """断言 _render_diff_preview 每个 diff 段的单列/双列配对行输出字节级一致。"""
    html = _render_diff_preview(diff_text)
    col_segs = _extract_container_all(html, "diff-seg-col")
    paired_segs = _extract_container_all(html, "diff-seg-paired")
    assert col_segs, "HTML 应包含 .diff-seg-col 段"
    assert len(col_segs) == len(paired_segs), f"单列/双列段数不一致: col={len(col_segs)}, paired={len(paired_segs)}"
    for idx, (col_seg, paired_seg) in enumerate(zip(col_segs, paired_segs)):
        col_pairs = _col_pair_codes(col_seg)
        paired_pairs = _paired_pair_codes(paired_seg)
        assert col_pairs == paired_pairs, (
            f"第 {idx} 段单列/双列配对行输出不一致\n  col:    {col_pairs}\n  paired: {paired_pairs}"
        )


class TestDualViewConsistency:
    """单列/双列双视图词对高亮输出一致性回归"""

    def test_single_pair_word_diff_consistent(self):
        """pair=1 + 词级差异（replace 命中）：单列/双列字节级一致"""
        diff = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def hello():\n"
            '-    return "old_value"\n'
            '+    return "new_value"\n'
        )
        _assert_dual_view_consistency(diff)

    def test_multi_pair_word_diff_consistent(self):
        """pair=5 多行 + 词级差异 → 单列/双列一致"""
        lines = ["--- a/foo.py", "+++ b/foo.py", "@@ -1,6 +1,6 @@", " def f():"]
        for i in range(5):
            lines.append(f'-    v{i} = "old_{i}" + x')
            lines.append(f'+    v{i} = "new_{i}" + x')
        diff = "\n".join(lines) + "\n"
        _assert_dual_view_consistency(diff)

    def test_no_word_diff_pair_consistent(self):
        """无词级差异（del/add token 相同 → SequenceMatcher 全 equal）"""
        diff = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def hello():\n"
            '-    return "same token text"\n'
            '+    return "same token text"\n'
        )
        _assert_dual_view_consistency(diff)

    def test_unpaired_lines_consistent(self):
        """del/add 数量不等（含 unpaired 行）→ 配对部分仍一致"""
        diff = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,5 +1,3 @@\n"
            " def f():\n"
            '-    a = "old_alpha"\n'
            '+    a = "new_alpha"\n'
            "-    removed_line_1\n"
            "-    removed_line_2\n"
            "+    added_only_line\n"
        )
        _assert_dual_view_consistency(diff)

    def test_fast_path_over_2000_chars_consistent(self):
        """>2000 字符 fast-path → 单列/双列一致"""
        long_old = "x = " + "a" * 2100
        long_new = "y = " + "b" * 2100
        diff = f"--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n def f():\n-    {long_old}\n+    {long_new}\n"
        _assert_dual_view_consistency(diff)

    def test_multiple_segments_all_consistent(self):
        """多个 diff hunk/段 → 每段单独一致"""
        diff = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def f():\n"
            '-    a = "one"\n'
            '+    a = "uno"\n'
            "@@ -10,2 +10,2 @@\n"
            " def g():\n"
            '-    b = "two"\n'
            '+    b = "dos"\n'
        )
        _assert_dual_view_consistency(diff)

    def test_empty_pair_zero_del_add_no_crash(self):
        """无配对行（diff 只有上下文）→ 不报错，且不产生差异段"""
        diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n def f():\n     pass\n"
        html = _render_diff_preview(diff)
        assert "diff-seg-col" not in html, "无增删行时不应产生差异段"
