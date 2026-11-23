# -*- coding: utf-8 -*-
"""vision 扫描/迁移共用的 blob 解析与块级统计。

格式定义只在 `app.core.store.serde` 一处：serde 写出的 BLOB 是
`ZSTD\\x01` + zstd frame（或 `JSON\\x01` + json），旧数据是无头 JSON。
**不能把整个 BLOB 直接交给 zstd 解压**——5 字节头会让解压必然失败。

被 `scan_base64_vision.py`（全文本口径 + 块级口径）与
`migrate_base64_vision.py`（块级迁移）共用，避免两处格式逻辑漂移。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.store.serde import deserialize  # noqa: E402

_ZSTD_FRAME_MAGIC = b"\x28\xb5\x2f\xfd"


def load_blob(raw: str | bytes | bytearray) -> Any | None:
    """serde BLOB → Python 对象；无法解析返回 None。

    依次尝试：serde（`ZSTD\\x01` / `JSON\\x01` / 无头旧 JSON）→ 裸 zstd 帧
    （无 serde 头的历史数据）→ None。裸帧解压后不是 JSON 时返回文本，调用方
    仍可对其做 `data:image` 子串检测。
    """
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if not data:
        return None
    try:
        return deserialize(data)
    except Exception:
        pass
    if data.startswith(_ZSTD_FRAME_MAGIC):
        try:
            from compression import zstd

            dec = zstd.ZstdDecompressor().decompress(data)
        except Exception:
            return None
        try:
            return json.loads(dec.decode("utf-8"))
        except Exception:
            return dec.decode("utf-8", "replace")
    return None


def blob_to_text(raw: str | bytes | bytearray) -> str:
    """BLOB → 文本（对象走 str，解析失败按 utf-8 replace 兜底）。"""
    if isinstance(raw, str):
        return raw
    obj = load_blob(raw)
    if obj is not None:
        return obj if isinstance(obj, str) else str(obj)
    return bytes(raw).decode("utf-8", "replace")


def inline_image_stats(messages: Any) -> tuple[int, int, set[str]]:
    """返回 (内嵌 data:image 块数, base64 正文字节数, 唯一 raw sha1[:16] 集合)。

    只认 ``content[i]`` 中 ``type=image_url`` 且 url 以 ``data:image`` 开头的块，
    与 `_persist_vision_image_blocks_impl` 的命中条件一致。
    """
    n_blocks = 0
    b64_bytes = 0
    uniq: set[str] = set()
    if not isinstance(messages, list):
        return 0, 0, uniq
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            img = block.get("image_url")
            url = img.get("url") if isinstance(img, dict) else None
            if not isinstance(url, str) or not url.startswith("data:image"):
                continue
            comma = url.find(",")
            if comma == -1:
                continue
            b64 = url[comma + 1 :]
            n_blocks += 1
            b64_bytes += len(b64)
            try:
                uniq.add(hashlib.sha1(base64.b64decode(b64)).hexdigest()[:16])
            except Exception:  # noqa: BLE001 — 坏块不计入唯一图
                continue
    return n_blocks, b64_bytes, uniq


def count_image_refs(messages: Any) -> int:
    """统计已是 image_ref 的块数（迁移幂等判据）。"""
    n = 0
    if not isinstance(messages, list):
        return n
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_ref":
                n += 1
    return n
