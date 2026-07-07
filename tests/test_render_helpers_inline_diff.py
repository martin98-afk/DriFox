# -*- coding: utf-8 -*-
"""测试 _render_diff_preview 单列视图的词级高亮与 fast-path。

回归目标：commit 91012ce3 引入双视图后，单列分支 (.diff-seg-col) 默认隐藏
了 .word-add/.word-del 字符级标识（必须手动切换到双列才看到）。修复后
默认单列视图也应输出词级 span。

覆盖场景：
1. pair>0：单列视图内必须同时含 .word-del 与 .word-add span，且行号正确
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
        """pair>0：单列视图内必须同时含 .word-del 与 .word-add span"""
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

        # 配对竖排：先 del 再 add（区别于修复前的"先全删后全增"）
        assert 'class="diff-line diff-del"' in seg_col, "应有 diff-del 行"
        assert 'class="diff-line diff-add"' in seg_col, "应有 diff-add 行"
        del_pos = seg_col.find('class="diff-line diff-del"')
        add_pos = seg_col.find('class="diff-line diff-add"')
        assert del_pos < add_pos, "单列应为配对竖排：del 在 add 之前"

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

        # 配对竖排仍然存在（修复后的结构）
        assert 'class="diff-line diff-del"' in seg_col
        assert 'class="diff-line diff-add"' in seg_col

        # fast-path：不生成词级 span（_highlight_code_line 直接染色）
        assert '<span class="word-del">' not in seg_col, ">2000 字符 fast-path 不应输出 .word-del span"
        assert '<span class="word-add">' not in seg_col, ">2000 字符 fast-path 不应输出 .word-add span"
