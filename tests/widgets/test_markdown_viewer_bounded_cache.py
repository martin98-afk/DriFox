# -*- coding: utf-8 -*-
"""[B2-补全] MarkdownBlockViewer（qt 渲染通道）的 _tool_md_cache 有界化验证。

- 封顶淘汰：写入超 32 条 → 条目数 == 32，最早键被淘汰
- 覆写不淘汰：对已存在键覆写不改条目数、不触发淘汰
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.widgets._bounded_caches import _MAX_TOOL_MD_ENTRIES, _BoundedToolMdCache
from app.widgets.markdown_block_viewer import MarkdownBlockViewer


def test_bounded_cache_cap_and_fifo():
    c = _BoundedToolMdCache()
    for i in range(_MAX_TOOL_MD_ENTRIES + 4):  # 写 36 条
        c[f"tid_{i}"] = f"<tool>{i}</tool>"
    assert len(c) == _MAX_TOOL_MD_ENTRIES == 32, "未封顶"
    assert "tid_0" not in c and "tid_35" in c, "最早键未被淘汰"


def test_overwrite_does_not_evict():
    c = _BoundedToolMdCache()
    for i in range(32):
        c[f"tid_{i}"] = f"<tool>{i}</tool>"
    assert len(c) == 32
    c["tid_10"] = "<tool>updated</tool>"  # 覆写已存在键
    assert len(c) == 32, "覆写触发淘汰"
    assert c["tid_10"] == "<tool>updated</tool>"


def test_markdown_block_viewer_uses_bounded_cache(qapp):
    """viewer 实例的 _tool_md_cache 是有界实现（qt 渲染通道不再无界）"""
    v = MarkdownBlockViewer()
    assert isinstance(v._tool_md_cache, _BoundedToolMdCache)
    for i in range(40):
        v._tool_md_cache[f"t{i}"] = "x"
    assert len(v._tool_md_cache) == 32


def test_bounded_lru_cap_and_touch():
    """[A3-2] 缩略图 LRU：超上限有界、最老淘汰、命中刷新热序"""
    from app.widgets._bounded_caches import _BoundedLRUCache

    c = _BoundedLRUCache(3)
    c["a"] = object(); c["b"] = object(); c["c"] = object()
    assert list(c) == ["a", "b", "c"]
    c.get("a")  # 命中触碰：a 变最新
    c["d"] = object()  # 超限 → 淘汰 b（最旧未触碰）
    assert list(c) == ["c", "a", "d"], "命中未刷新热序或淘汰了非最旧"

    pix_cache = _BoundedLRUCache(32)
    for i in range(40):
        pix_cache[f"/p/{i}.png"] = object()
    assert len(pix_cache) == 32, "缩略图缓存未封顶"
    assert "/p/0.png" not in pix_cache and "/p/39.png" in pix_cache
