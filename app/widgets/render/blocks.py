# -*- coding: utf-8 -*-
"""Block 级渲染的数据模型与增量补丁协议。

纯数据 + 纯函数，不构造任何 QWidget，可在任意线程调用。

三件事：

1. ``BlockRender`` 是一条消息渲染后的最小单元，**一个 DOM 节点对应一个实例**。
   这与既有管线「整条消息 → 一整段 HTML → 整页 innerHTML 替换」的根本区别，
   也是「稳定区」不再需要猜测的原因：block 列表本身就是稳定区。
2. ``key`` 是内容指纹，与位置无关。流式期间只有**活动块**的 key 会变，
   历史块全部稳定 → ``diff_renders`` 的前缀匹配直接跳过它们。
3. ``home`` 指定节点归属容器，取代 JS 侧 ``reorganizeContent`` 的事后搬移。

设计文档：docs/superpowers/specs/2026-11-02-block-render-pipeline-design.md
本轮只落地纯函数层，不接任何现有调用点。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Sequence

KIND_TEXT = "text"
KIND_REASONING = "reasoning"
KIND_TOOL = "tool"
KIND_IMAGE = "image"
KIND_CUSTOM = "custom"

# 归属容器：正文区 / 「工具与思考」折叠区
HOME_CONTENT = "content"
HOME_TOOL = "tool"


def fingerprint(*parts: Any) -> str:
    """稳定内容指纹（12 位 sha1）。

    用 sha1 而非内置 ``hash()``：后者受 PYTHONHASHSEED 影响，跨进程不稳定，
    会导致同一份历史消息在重启后拿到不同 key（DOM 无法复用、调试时对不上）。
    """
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()[:12]


@dataclass(frozen=True)
class RenderCtx:
    """渲染上下文（一次渲染内不可变）。

    ``streaming`` 决定协议段是否可能未闭合；``compact`` 是简洁模式；
    ``lazy_body`` 让折叠态工具块 body 只出空占位；``heavy_caps`` 是历史卡的
    重负载截断上限 (result, diff, echarts)。
    """

    streaming: bool = False
    compact: bool = False
    lazy_body: bool = False
    heavy_caps: tuple[int, int, int] | None = None
    # 已完成工具的协议段指纹集合：流式期未闭合协议段命中时丢弃，
    # 避免模型照抄协议文本产生的重复占位框（沿用 _inject_tool_blocks 语义）
    tool_fingerprints: frozenset[str] = frozenset()
    pygments_style: str = "dracula"
    icon_prefix: str = ""
    code_font_size: int = 13


@dataclass(frozen=True)
class BlockRender:
    """一个待落地 DOM 节点的完整描述。

    ``identity`` 是 DOM 复用判据：三个字段全等价才算「同一个节点、无需改动」。
    仅比 key 不比 kind/home 会让「同内容不同归属」的块被误判为未变化。
    """

    kind: str
    key: str
    html: str
    home: str = HOME_CONTENT

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.kind, self.home, self.key)


@dataclass(frozen=True)
class PatchOps:
    """容器子节点的 splice 指令：从 ``base`` 起删 ``delete`` 个，插入 ``items``。"""

    base: int
    delete: int
    items: tuple[BlockRender, ...]

    @property
    def is_empty(self) -> bool:
        return self.delete == 0 and not self.items

    def to_payload(self) -> dict:
        """JS 侧 ``patchBlocks`` 的入参形态。"""
        return {
            "base": self.base,
            "delete": self.delete,
            "items": [{"kind": it.kind, "key": it.key, "html": it.html, "home": it.home} for it in self.items],
        }


def diff_renders(old: Sequence[BlockRender], new: Sequence[BlockRender]) -> PatchOps:
    """按 identity 计算公共前缀/后缀，返回最小 splice 指令。

    与既有 ``_extract_closed_segments`` 的思路区别：那边在 markdown 字符串里
    找 ``\\n\\n`` 边界并判定 fence / think / tool 是否闭合，任何一处判错就把
    半截内容当完整段产出（P004 / P003 一族的根因）；这边只比 block 指纹，
    闭合判断由 block 列表本身保证，不存在「猜错」这个失效模式。

    流式的典型结果：前缀 = 全部历史块，后缀 = 0，delete = 1，items = 1 个新
    活动块。即每批只替换一个节点的内容。
    """
    old_n = len(old)
    new_n = len(new)
    limit = min(old_n, new_n)

    prefix = 0
    while prefix < limit and old[prefix].identity == new[prefix].identity:
        prefix += 1

    suffix = 0
    while suffix < limit - prefix and old[old_n - 1 - suffix].identity == new[new_n - 1 - suffix].identity:
        suffix += 1

    tail = new_n - suffix if suffix else new_n
    return PatchOps(base=prefix, delete=old_n - prefix - suffix, items=tuple(new[prefix:tail]))
