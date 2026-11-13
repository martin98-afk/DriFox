# -*- coding: utf-8 -*-
"""IM 文本切分：按自然边界切片，供各平台适配器复用。

规则对齐 dsh-im（``editable-message-stream.mjs::splitMessageText``）：
优先在换行处切，其次空格，都不行才硬切；切点不早于 ``min_ratio`` 比例，
避免切出只有一两个字的孤片。平台只需给出自己的单条上限。
"""
from __future__ import annotations

from typing import List


def split_message(
    text: str,
    limit: int,
    *,
    min_ratio: float = 0.55,
    prefer_paragraph: bool = False,
) -> List[str]:
    """把长文本切成每片不超过 ``limit`` 的若干片。

    Args:
        text: 原文
        limit: 单条上限（字符数），<=0 时原样返回一条
        min_ratio: 自然边界离片尾的最小比例，越界则改用下一级边界或硬切
        prefer_paragraph: 优先在空行（段落）处切，适合阅读型长文

    Returns:
        切片列表；空文本返回空列表
    """
    body = (text or "").strip()
    if not body:
        return []
    if limit <= 0 or len(body) <= limit:
        return [body]

    floor = int(limit * min_ratio)
    chunks: List[str] = []
    remaining = body
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit) if prefer_paragraph else -1
        if cut < floor:
            cut = remaining.rfind("\n", 0, limit)
        if cut < floor:
            cut = remaining.rfind(" ", 0, limit)
        if cut < floor:
            cut = limit
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
