# -*- coding: utf-8 -*-
"""块级渲染缓存：已定型块的 HTML 不再重算。

为什么需要它
------------
``render_blocks`` 每次都对全部 block 跑一遍渲染。流式期间每批只改动活动块，
但历史块（工具结果、思考块、已完成的正文段）会被反复重算 —— 这就是既有管线
「每批重扫整条消息」的 O(n²) 成本来源，换成 block 切分只是把它变成「每批重渲
全部块」，量级没变。

缓存按 ``BlockRender.key`` 索引：key 是内容指纹，内容不变则 key 不变，直接命中。
活动块的 key 带 completed 维度，且文本长度每批都变 → 天然不命中，无需额外失效。

线程安全：缓存挂在调用方传入的实例上（每个 viewer 一个），不跨 viewer 共享，
避免不同渲染参数（compact / lazy_body / 主题）互相污染。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict

from app.widgets.render.blocks import BlockRender

# 每卡缓存上限。消息 block 数实测最多 11（全库统计），128 足够覆盖任何真实会话；
# 超限按 LRU 淘汰，避免异常输入（如把整段历史拼成一条消息）撑爆内存。
_MAX_ENTRIES = 128


class BlockCache:
    """按内容指纹缓存 BlockRender（LRU）。"""

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._items: "OrderedDict[str, BlockRender]" = OrderedDict()
        self._max = max_entries
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> BlockRender | None:
        item = self._items.get(key)
        if item is None:
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return item

    def put(self, key: str, value: BlockRender) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def stats(self) -> Dict[str, int]:
        return {"size": len(self._items), "hits": self.hits, "misses": self.misses}
