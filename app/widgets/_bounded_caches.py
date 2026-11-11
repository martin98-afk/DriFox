# -*- coding: utf-8 -*-
"""有界缓存放置点：被多个 viewer（webview / qt 渲染通道）共享的无界缓存治理件。

放独立模块的原因：card_viewers.py 顶部拉 QWebEngineWidgets，而 qt 渲染通道
（markdown_block_viewer）的存在意义就是脱离 WebEngine 依赖——若从 card_viewers
import 本类会把 WebEngine DLL 链拉进 Qt 通道，故此处只放无依赖的纯数据结构。
"""
from collections import OrderedDict
from typing import Dict

# [B2] 工具 markdown 懒缓存条目上限：写入方在 MessageCard 直接 []= 追加，
# 只能以有界 dict 收敛。取 32 ≈ 单轮工具块数粗估（≤11）的 3 倍余量，且每轮
# 流式结束已有 clear()，单轮内条目数 = 实际工具块数，正常使用永不触发淘汰；
# 触发时淘汰后果仅是命中缺失后按需重转换（content_to_markdown 单块，等价
# 输出，毫秒级）。
_MAX_TOOL_MD_ENTRIES = 32


class _BoundedToolMdCache(dict):
    """[B2] 有界工具 markdown 缓存：写入超上限按插入序淘汰最旧键。

    读路径（MessageCard cache.get）不经本类维护热序（FIFO 可接受：读侧在
    禁区文件，写侧有界是唯一可行位）；淘汰发生在超上限写入时。
    """

    def __setitem__(self, key, value) -> None:
        if key not in self and len(self) >= _MAX_TOOL_MD_ENTRIES:
            del self[next(iter(self))]  # dict 保插入序：最早键即最旧
        super().__setitem__(key, value)


class _BoundedLRUCache(OrderedDict):
    """[A3-2] 有界 LRU 缓存：写入超上限淘汰最旧（最久未触碰），命中触碰热序。

    用于缩略图一类「键=路径、值=QPixmap」的存活期内只增缓存（读侧同文件
    可触碰热序，与 _BoundedToolMdCache 的 FIFO 不同）。
    """

    def __init__(self, max_entries: int = 32):
        super().__init__()
        self._max_entries = max_entries

    def __setitem__(self, key, value) -> None:
        if key in self:
            self.move_to_end(key)
        elif len(self) >= self._max_entries:
            self.popitem(last=False)
        super().__setitem__(key, value)

    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
        return super().get(key, default)
