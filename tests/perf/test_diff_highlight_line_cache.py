# -*- coding: utf-8 -*-
"""行级 diff 高亮缓存（app/utils/diff_highlight.py）回归与微基准。

被测改动：``_highlight_code_line`` 走 ``_highlight_code_line_cached``
（LRU 4096，key = 文本 + lexer + 当前风格）。

为什么值：``_render_diff_preview`` 对同一段文本重复高亮 —— 单列视图与双列
视图各一份、词级 diff 的 equal 段在 old/new 两侧各一份。500 行 diff 实测
1499 次调用中 993 次同参重复，逐行 Pygments lexer 占单卡渲染 89%。缓存后
单卡冷渲染 40ms → 21ms（本项目 2026-11-27 实测，见下方微基准 print）。

断言口径：
- 等价性用确定性断言（缓存产物 == 直算产物、截断/转义/异常分支一致）；
- 命中行为用 pygments.highlight 调用计数验证（同参第二次不再进 lexer）；
- 性能只 print 实测毫秒，不设阈值（数值随机器漂移，仅供对照采集）。
"""

import statistics
import time

import pytest

from app.utils import diff_highlight as dh
from app.widgets.render_helpers import _render_diff_preview


def _make_diff(n: int) -> str:
    """构造 n 行 unified diff（py 文件，走真 lexer 而非 TextLexer）。"""
    lines = ["--- a/app/widgets/card_viewers.py", "+++ b/app/widgets/card_viewers.py"]
    for i in range(n // 5):
        lines.append(f"@@ -{i * 10 + 1},8 +{i * 10 + 1},9 @@ def handler_{i}(value):")
        lines.append(f"     result = compute_{i}(value, mode='fast')")
        lines.append(f"-    old_value_{i} = legacy_transform(value, factor={i})")
        lines.append(f"+    new_value_{i} = modern_transform(value, factor={i}, flags=0x{i:02x})")
        lines.append(f"     return result + offset_{i}")
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _clean_cache():
    dh._highlight_code_line_cached.cache_clear()
    yield
    dh._highlight_code_line_cached.cache_clear()


class TestHighlightLineCache:
    def test_cached_output_equals_direct_highlight(self):
        """缓存产物与直算逐字相同（等价性核心断言）。"""
        lexer = dh._get_diff_lexer("handler.py")
        samples = [
            "    new_value = modern_transform(value, factor=42, flags=0x2a)",
            "def handler(self, event):  # 中文注释",
            'raise ValueError("boom")',
            "x = [i ** 2 for i in range(10)]",
        ]
        for text in samples:
            expected = dh._pyg_highlight(text, lexer, dh._get_diff_formatter()).rstrip("\n")
            assert dh._highlight_code_line(text, lexer) == expected

    def test_second_call_skips_pygments(self, monkeypatch):
        """同参第二次调用不再进入 pygments.highlight。"""
        calls = []
        real = dh._pyg_highlight
        monkeypatch.setattr(dh, "_pyg_highlight", lambda *a, **k: calls.append(1) or real(*a, **k))
        lexer = dh._get_diff_lexer("handler.py")
        dh._highlight_code_line("value = transform(1)", lexer)
        first = len(calls)
        dh._highlight_code_line("value = transform(1)", lexer)
        assert first == 1
        assert len(calls) == 1, "同参重复调用应命中缓存，不再进 pygments"

    def test_style_change_isolated(self, monkeypatch):
        """风格切换后不返回旧风格产物（style 在 key 内）。"""
        calls = []
        real = dh._pyg_highlight
        monkeypatch.setattr(dh, "_pyg_highlight", lambda *a, **k: calls.append(1) or real(*a, **k))
        lexer = dh._get_diff_lexer("handler.py")
        old_style = dh._current_diff_style
        try:
            dh.set_diff_highlight_style("dracula")
            dh._highlight_code_line("value = transform(1)", lexer)
            n1 = len(calls)
            dh.set_diff_highlight_style("friendly")
            dh._highlight_code_line("value = transform(1)", lexer)
            assert len(calls) == n1 + 1, "风格变化必须重新高亮"
            dh.set_diff_highlight_style("dracula")
            dh._highlight_code_line("value = transform(1)", lexer)
            assert len(calls) == n1 + 1, "切回旧风格应命中旧风格缓存"
        finally:
            dh.set_diff_highlight_style(old_style)

    def test_plain_lexer_bypasses_cache(self):
        """纯文本 lexer 走 escape 短路，不占缓存。"""
        before = dh._highlight_code_line_cached.cache_info().currsize
        assert dh._highlight_code_line("<b>raw</b>", dh._TEXT_LEXER) == "&lt;b&gt;raw&lt;/b&gt;"
        assert dh._highlight_code_line("<b>raw</b>", None) == "&lt;b&gt;raw&lt;/b&gt;"
        assert dh._highlight_code_line_cached.cache_info().currsize == before

    def test_long_line_not_retained(self):
        lexer = dh._get_diff_lexer('settings.json')
        text = '[' + ', '.join(str(i) for i in range(750)) + ']'
        expected = dh._pyg_highlight(text, lexer, dh._get_diff_formatter()).rstrip('\n')
        assert dh._highlight_code_line(text, lexer) == expected
        assert dh._highlight_code_line_cached.cache_info().currsize == 0

    def test_cache_bounded(self):
        """缓存条目数不超过上限（LRU 淘汰生效）。"""
        lexer = dh._get_diff_lexer("handler.py")
        for i in range(dh._HL_CACHE_MAX + 200):
            dh._highlight_code_line(f"value_{i} = transform({i})", lexer)
        info = dh._highlight_code_line_cached.cache_info()
        assert info.currsize <= dh._HL_CACHE_MAX


class TestRenderDiffPreviewEquivalence:
    def test_full_html_identical_to_uncached_render(self):
        """整段 diff 渲染产物与「无缓存直算」实现逐字相同。"""
        diff = _make_diff(300)

        cached_html = _render_diff_preview(diff)
        assert cached_html  # 非空

        # 对照：把行级高亮替换成无缓存直算，其余逻辑不变
        real_cached = dh._highlight_code_line_cached

        def _direct(text, lexer, style):
            return dh._pyg_highlight(text, lexer, dh._get_diff_formatter(style)).rstrip("\n")

        dh._highlight_code_line_cached = _direct
        try:
            direct_html = _render_diff_preview(diff)
        finally:
            dh._highlight_code_line_cached = real_cached

        assert cached_html == direct_html


@pytest.mark.perf
def test_bench_render_diff_preview(capsys):
    """微基准：500 行 diff 单卡渲染（冷启动 / 缓存命中），只 print 不设阈值。"""
    diff = _make_diff(498)

    def _rounds(clear=None) -> list:
        times = []
        for _ in range(5):
            if clear is not None:
                clear()
            t0 = time.perf_counter()
            _render_diff_preview(diff)
            times.append((time.perf_counter() - t0) * 1000)
        return times

    # 对照：禁用缓存（替换成直算），给出同机同进程的改前基线。
    # 放在最前跑，避免前两轮已把 pygments 内部状态预热后高估基线。
    real_cached = dh._highlight_code_line_cached

    def _direct(text, lexer, style):
        return dh._pyg_highlight(text, lexer, dh._get_diff_formatter(style)).rstrip("\n")

    dh._highlight_code_line_cached = _direct
    try:
        uncached = _rounds()
    finally:
        dh._highlight_code_line_cached = real_cached

    # 冷启动：每轮清缓存，等价于每张卡首次进入渲染
    cold = _rounds(clear=dh._highlight_code_line_cached.cache_clear)
    # 热：同卡重复渲染（卡重建 / 切简洁模式后的第二轮）
    warm = _rounds()

    with capsys.disabled():
        print(
            f"\n[perf] diff 渲染 498 行: 无缓存 p50={statistics.median(uncached):.2f}ms "
            f"(min={min(uncached):.2f}) / 冷 p50={statistics.median(cold):.2f}ms "
            f"(min={min(cold):.2f}) / 热 p50={statistics.median(warm):.2f}ms "
            f"/ cache={dh._highlight_code_line_cached.cache_info()}"
        )
    assert len(_render_diff_preview(diff)) > 0
