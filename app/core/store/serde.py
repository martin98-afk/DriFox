# -*- coding: utf-8 -*-
"""
压缩 JSON 序列化层 (PEP 784 zstd)

提供透明的双向序列化能力，自动处理：
- 新数据：zstd 压缩 + 格式魔数
- 旧数据：无魔数原始 JSON（向后兼容）

设计要点：
- 4-byte magic + 1-byte version + payload 格式
- 解压失败时自动 fallback 到原始 JSON 解析（最大限度向后兼容）
- 3.14+ 强制使用 compression.zstd，无降级路径（项目已锁定 Python 3.14）

PEP 784 zstd API 注意：
- ZstdCompressor 是 streaming API，每次压缩需要：
  1. set_pledged_input_size(len(data))
  2. compress(data) → 部分输出
  3. flush() → frame 结束标记 + 剩余输出
- ZstdDecompressor.decompress() 接受完整 frame，一次性返回完整数据
"""

from __future__ import annotations

from compression import zstd  # PEP 784, Python 3.14+
from typing import Any

import orjson

# 格式魔数常量
_MAGIC_ZSTD = b"ZSTD"  # 新版 zstd 压缩数据
_MAGIC_JSON = b"JSON"  # 新版未压缩 JSON（预留，暂未启用写入路径）
_VERSION_V1 = b"\x01"  # 协议版本号（当前为 1）

# 当前写入使用的压缩级别（zstd 1-22）
# 3 是 sweet spot：压缩速度比 gzip 快 3-5 倍，压缩率相当
DEFAULT_COMPRESSION_LEVEL = 3


def _zstd_compress(raw: bytes) -> bytes:
    """
    zstd 压缩（PEP 784 streaming API 封装）

    Args:
        raw: 原始字节

    Returns:
        完整 zstd frame 字节流
    """
    compressor = zstd.ZstdCompressor(level=DEFAULT_COMPRESSION_LEVEL)
    compressor.set_pledged_input_size(len(raw))
    return compressor.compress(raw) + compressor.flush()


def _zstd_decompress(payload: bytes) -> bytes:
    """
    zstd 解压（PEP 784 streaming API 封装）

    Args:
        payload: 完整 zstd frame 字节流

    Returns:
        原始字节
    """
    decompressor = zstd.ZstdDecompressor()
    return decompressor.decompress(payload)


def serialize(data) -> bytes:
    """
    序列化数据为带格式魔数的字节流

    流程：orjson.dumps → bytes → zstd.compress → magic + version + payload

    Args:
        data: 任意可 JSON 序列化的对象（dict / list / str / int / None 等）

    Returns:
        bytes: 可直接存进 SQLite 的字节流
    """
    raw = orjson.dumps(data)
    compressed = _zstd_compress(raw)
    return _MAGIC_ZSTD + _VERSION_V1 + compressed


def deserialize(data) -> Any:
    """
    反序列化字节流为 Python 对象

    透明支持三种格式：
    - ZSTD + version（新压缩数据）
    - JSON + version（预留，新未压缩数据）
    - 原始 JSON（无 magic，旧数据，向后兼容）

    Args:
        data: SQLite 返回的 bytes / str / None

    Returns:
        反序列化后的 Python 对象；data 为 None / 空时返回 None
    """
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data:
        return None

    # 格式 A：新版 zstd 压缩
    if data.startswith(_MAGIC_ZSTD):
        version = data[4:5]
        if version != _VERSION_V1:
            raise ValueError(
                f"[serde] Unsupported zstd payload version: {version!r} "
                f"(expected {_VERSION_V1!r})"
            )
        payload = data[5:]
        raw = _zstd_decompress(payload)
        return orjson.loads(raw)

    # 格式 B：新版未压缩 JSON（预留）
    if data.startswith(_MAGIC_JSON):
        version = data[4:5]
        if version != _VERSION_V1:
            raise ValueError(
                f"[serde] Unsupported JSON payload version: {version!r} "
                f"(expected {_VERSION_V1!r})"
            )
        payload = data[5:]
        return orjson.loads(payload)

    # 格式 C：旧数据（无 magic，直接 orjson 解析，最大限度向后兼容）
    return orjson.loads(data)


def is_compressed(data) -> bool:
    """
    检测数据是否被 zstd 压缩

    Args:
        data: bytes / str

    Returns:
        bool: True 表示 zstd 压缩格式
    """
    if data is None:
        return False
    if isinstance(data, str):
        data = data.encode("utf-8")
    return data.startswith(_MAGIC_ZSTD)


def compression_stats(data) -> dict:
    """
    计算压缩率（用于调试和统计）

    Args:
        data: 待压缩的 Python 对象

    Returns:
        dict: {raw_size, compressed_size, ratio, magic}
    """
    raw = orjson.dumps(data)
    compressed = _zstd_compress(raw)
    return {
        "raw_size": len(raw),
        "compressed_size": len(_MAGIC_ZSTD) + len(_VERSION_V1) + len(compressed),
        "payload_size": len(compressed),
        "ratio": 1 - len(compressed) / len(raw) if raw else 0,
        "magic": _MAGIC_ZSTD.decode("ascii"),
    }
