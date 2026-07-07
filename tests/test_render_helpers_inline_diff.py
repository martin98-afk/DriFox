# -*- coding: utf-8 -*-
"""测试 _render_diff_preview 单列视图的词级高亮与 fast-path。

回归目标：
1. commit 91012ce3 引入双视图后，单列分支 (.diff-seg-col) 默认隐藏
   了 .word-add/.word-del 字符级标识（必须手动切换到双列才看到）。修复后
   默认单列视图也应输出词级 span。
2. 单列视图布局为"所有删除行先、所有新增行后"（连续堆叠，不再
   del/add 交替），同时保留词级差异高亮。

覆盖场景：
1. pair>0：单列视图内必须同时含 .word-del 与 .word-add span，
   行号正确，且所有 del 集中在所有 add 之前
2. pair=0（纯删/纯增）：走未配对 fallback，无词级 span
3. >2000 字符：触发 fast-path 返回纯语法高亮，无词级 span
"""

from app.widgets.render_helpers import _render_diff_preview


def _extract_seg_col(html: str) -> str:
    """提取第一段 .diff-seg-col（单列视图）的 HTML 片段。

    .diff-seg-col 内嵌套多个 <div class="diff-line">，用嵌套计数器找到
    匹配的容器闭标签。深度初始为 1（含容器自身），每次 <div 加 1、
    </div> 减 1，回到 0 时即容器闭合。
    """
    start = html.find('<div class="diff-seg-col">')
    assert start != -1, "HTML 必须包含 .diff-seg-col 容器"
    pos = start
    depth = 1  # 容器自身
    while pos < len(html):
        next_open = html.find("<div", pos + 1)
        next_close = html.find("</div>", pos + 1)
        if next_close == -1:
            break
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open
        else:
            depth -= 1
            pos = next_close
            if depth == 0:
                return html[start : pos + len("</div>")]
    raise AssertionError("未找到 .diff-seg-col 的容器闭标签")


class TestInlineDiffSingleColumn:
    """_render_diff_preview 单列视图词级高亮回归"""

    def test_pair_lines_have_word_markers_in_single_column(self):
        """pair>0：单列视图内必须同时含 .word-del 与 .word-add span

        布局约束：所有删除行连续堆叠在最前，所有新增行连续堆叠在最后
        （区别于修复前的"先全删后全增"以及中间状态的"配对竖排 del/add 交替"）。
        """
        diff = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def hello():\n"
            '-    return "old_value"\n'
            '+    return "new_value"\n'
        )
        html = _render_diff_preview(diff)
        seg_col = _extract_seg_col(html)

        # 所有删除行先、所有新增行后（连续堆叠）
        assert 'class="diff-line diff-del"' in seg_col, "应有 diff-del 行"
        assert 'class="diff-line diff-add"' in seg_col, "应有 diff-add 行"
        first_del = seg_col.find('class="diff-line diff-del"')
        first_add = seg_col.find('class="diff-line diff-add"')
        assert first_del < first_add, "单列布局：所有 del 行应位于所有 add 行之前"

        # 强约束：最后一个 del 行的位置早于第一个 add 行（即 del 与 add 不交错）
        last_del = seg_col.rfind('class="diff-line diff-del"')
        assert last_del < first_add, "单列布局：所有 del 行必须连续堆叠在所有 add 行之前（不允许 del/add 交替）"

        # 核心断言：词级标识在单列视图中可见
        assert '<span class="word-del">' in seg_col, "单列 .diff-seg-col 必须包含 .word-del span（修复目标）"
        assert '<span class="word-add">' in seg_col, "单列 .diff-seg-col 必须包含 .word-add span（修复目标）"

        # 配对后行号仍正确（旧行号 2 → 新行号 2）。精确匹配 class="line-num">2<
        # 避免 ">2</span>" 误匹配以 2 结尾的其他行号（12/22/102 等）
        assert 'class="line-num">2<' in seg_col, "旧行行号应为 2"

    def test_zero_pair_falls_back_to_syntax_only(self):
        """pair=0：纯删/纯增段不应有词级 span，只有语法高亮 fallback"""
        diff = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,1 @@\n"
            " def hello():\n"
            '-    return "old_value1"\n'
            '-    return "old_value2"\n'
        )
        html = _render_diff_preview(diff)
        seg_col = _extract_seg_col(html)

        # 全是 del，没有 add
        assert 'class="diff-line diff-del"' in seg_col
        assert 'class="diff-line diff-add"' not in seg_col, "纯删段不应出现 diff-add 行"

        # pair=0 走未配对 fallback，无词级标识
        assert '<span class="word-del">' not in seg_col, "pair=0 时不应有 .word-del span（仅 _highlight_code_line）"
        assert '<span class="word-add">' not in seg_col

    def test_long_text_takes_fast_path(self):
        """>2000 字符：走 fast-path 返回纯语法高亮，无词级 span"""
        # 构造 add 行长度 > 2000 字符，触发 _highlighted_word_diff_html 的 fast-path
        long_text = "x = " + "a" * 2100
        diff = (
            '--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n def hello():\n-    return "old_value"\n+    '
            + long_text
            + "\n"
        )
        html = _render_diff_preview(diff)
        seg_col = _extract_seg_col(html)

        # 布局仍然满足"所有 del 在前、所有 add 在后"
        assert 'class="diff-line diff-del"' in seg_col
        assert 'class="diff-line diff-add"' in seg_col
        first_del = seg_col.find('class="diff-line diff-del"')
        first_add = seg_col.find('class="diff-line diff-add"')
        assert first_del < first_add, ">2000 字符 fast-path 也应保持 del→add 布局"

        # fast-path：不生成词级 span（_highlight_code_line 直接染色）
        assert '<span class="word-del">' not in seg_col, ">2000 字符 fast-path 不应输出 .word-del span"
        assert '<span class="word-add">' not in seg_col, ">2000 字符 fast-path 不应输出 .word-add span"
