# -*- coding: utf-8 -*-
"""card_render_core 块粒度高亮产物缓存与单块 4000 降级回归（R6）

T4 实测：60KB 文档 formalize/v1 重渲时 pygments 整块高亮 84.8ms，占整帧 92%。
本文件断言：
1. _highlight_block_cached 二次调用命中缓存且产物一致（等价性）
2. 超 _TAIL_CODE_WRAP_MAX_CHARS 代码块单块降级：escape 纯文本、内容完整非截断、
   容器/行号/复制按钮保留
3. 正常规模块/formalize 产物仍带 pygments 高亮标记
4. 缓存 key 隔离：不同 formatter 实例各算各的（worker 与主线程不互相污染）
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtGui")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _make_py_code(lines: int) -> str:
    return "".join(
        f"def process_item_{i}(payload, options=None):\n"
        f"    result = payload.get('key_{i}', 0) * 2 + len(options or {{}})\n"
        f"    if result > 100:\n        return {{'status': 'ok', 'value': result}}\n"
        f"    return None\n\n"
        for i in range(lines)
    )


def test_highlight_cache_hit_identical():
    """同一(内容, lang, formatter)二次调用：产物一致且命中缓存"""
    from app.widgets.card_render_core import _highlight_block_cached, _get_formatter_cached

    code = _make_py_code(30)
    fmt = _get_formatter_cached()
    _highlight_block_cached.cache_clear()

    first = _highlight_block_cached(code, "python", fmt)
    info_after_first = _highlight_block_cached.cache_info()
    second = _highlight_block_cached(code, "python", fmt)
    info_after_second = _highlight_block_cached.cache_info()

    assert first == second, "缓存命中产物必须一致"
    assert info_after_first.misses == 1, "首次调用应真实计算"
    assert info_after_second.hits >= 1, "二次调用必须命中缓存"
    assert "<span" in first, "正常规模块应含 pygments 高亮标记"


def test_oversize_block_degraded_but_complete():
    """超 4000 字符代码块：单块降级为 escape 纯文本，内容完整、容器与复制按钮保留"""
    from app.widgets.card_render_core import (
        _TAIL_CODE_WRAP_MAX_CHARS,
        _wrap_code_blocks_with_copy_button_web,
        get_markdown_instance,
    )

    code = _make_py_code(220)  # 220 组 ≈ 7000+ 字符，必超 4000
    assert len(code) > _TAIL_CODE_WRAP_MAX_CHARS
    marker_head = "def process_item_0(payload, options=None):"
    marker_tail = f"def process_item_{219}(payload, options=None):"

    md = get_markdown_instance()
    md.reset()
    html = md.convert(f"```python\n{code}```")
    wrapped = _wrap_code_blocks_with_copy_button_web(html)

    assert 'data-action="copy"' in wrapped, "复制按钮必须保留"
    assert "line-numbers" in wrapped, "行号容器必须保留"
    assert marker_head in wrapped, "降级后内容必须完整（头部）"
    assert marker_tail in wrapped, "降级后内容必须完整（尾部，非截断）"
    # pygments token class（单字母类名）不应出现；复制按钮图标 span 是 class="ci"，不冲突
    assert 'class="k"' not in wrapped, "降级块不应含 pygments 高亮 span"


def test_normal_block_and_formalize_keep_highlight():
    """正常规模块（含 formalize 整块渲染链路）产物仍带高亮标记"""
    from app.widgets.card_render_core import _wrap_code_blocks_with_copy_button_web, get_markdown_instance
    from app.widgets.render.block_render import _render_text_block
    from app.widgets.render.blocks import RenderCtx

    code = _make_py_code(30)
    md = get_markdown_instance()
    md.reset()
    html = md.convert(f"```python\n{code}```")
    wrapped = _wrap_code_blocks_with_copy_button_web(html)
    assert "<span class=" in wrapped, "4000 以内块必须保留 pygments 高亮"
    assert "code-container" in wrapped

    ctx = RenderCtx()
    seg_html, _ = _render_text_block(f"```python\n{code}```", ctx, True, 0)
    assert "code-container" in seg_html, "formalize 整块渲染必须含代码容器"
    assert "<span class=" in seg_html, "formalize 整块渲染必须含 pygments 高亮标记"


def test_cache_key_isolated_by_formatter():
    """不同 formatter 实例不共享缓存条目（key 含 formatter，防跨线程产物污染）"""
    from pygments.formatters import HtmlFormatter

    from app.widgets.card_render_core import _highlight_block_cached

    code = _make_py_code(10)
    fmt_a = HtmlFormatter(style="dracula", noclasses=False)
    fmt_b = HtmlFormatter(style="friendly", noclasses=False)
    _highlight_block_cached.cache_clear()

    _highlight_block_cached(code, "python", fmt_a)
    _highlight_block_cached(code, "python", fmt_b)
    info = _highlight_block_cached.cache_info()

    assert info.misses == 2, "两个不同 formatter 实例应各自 miss（key 隔离）"
    out_a = _highlight_block_cached(code, "python", fmt_a)
    out_b = _highlight_block_cached(code, "python", fmt_b)
    assert _highlight_block_cached.cache_info().hits >= 2, "各自二次调用应命中"
    # 注：noclasses=False 的 class 化产物与 style 无关（颜色在主题 CSS，B3 语义），
    # 不同 style 产物逐字相同属预期；key 隔离由 misses==2 + 各自 hits 证明。
