# -*- coding: utf-8 -*-
"""图像编解码工具（与对话/工作线程解耦，供 store 层复用）。

从 ``app.core.workers.chat_worker`` 抽出：``revive_vision_image_refs``（store 层）
需要压缩超大 data URI，直接反向 import workers 会形成 store → workers 的反向依赖。
"""

from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from PyQt5.QtCore import QBuffer, QByteArray, QIODevice
from PyQt5.QtGui import QImage

# ── B1：压缩结果 LRU（内容寻址，跨轮/跨 worker 命中）──
# 键 = data URI 摘要（sha1），值 = 压缩后 data URI。同一落盘文件每次 revive
# 生成的 data URI 字节相同 → 摘要相同 → 第二轮起直接命中，不再重复 QImage
# 解码/缩放/编码（实测单张 >5MB 图压缩瞬时峰值 = 输入 2.75 倍，重复触发代价高）。
_COMPRESS_CACHE: "OrderedDict[str, str]" = OrderedDict()
_COMPRESS_CACHE_MAX = 4
_COMPRESS_CACHE_LOCK = Lock()


def _data_uri_digest(data_uri: str) -> str:
    """data URI 稳定摘要（LRU 键与 revoke 的压缩图判定共用，保证口径一致）。"""
    import hashlib

    return hashlib.sha1(data_uri.encode("ascii", "ignore")).hexdigest()


def _compress_impl(data_uri: str, max_bytes: int) -> str:
    """compress_data_uri 的原压缩体（抽出让 LRU 命中率可测）。"""
    # 解析 data URI
    header_end = data_uri.find(",")
    if header_end == -1:
        return data_uri
    b64_data = data_uri[header_end + 1 :]

    try:
        import base64

        img_bytes = base64.b64decode(b64_data)
        img = QImage.fromData(QByteArray(img_bytes))
        del img_bytes
        if img.isNull():
            logger.warning("[Vision] QImage 解码失败，跳过压缩")
            return data_uri

        orig_w, orig_h = img.width(), img.height()
        logger.warning(f"[Vision] 图片过大: base64={len(b64_data)} bytes, 原始尺寸={orig_w}x{orig_h}，开始压缩...")

        # 等比缩小：按面积比例估算缩放因子，留 20% 余量避免反复压缩
        # base64 ≈ 原始字节 × 4/3，原始字节 ≈ b64_data × 3/4
        raw_size_est = len(b64_data) * 3 // 4
        target_raw = max_bytes * 3 // 4
        scale = (target_raw / raw_size_est) ** 0.5 * 0.8  # 面积平方根 + 余量
        scale = max(0.1, min(1.0, scale))  # 限制在 [0.1, 1.0]

        new_w = max(64, int(orig_w * scale))
        new_h = max(64, int(orig_h * scale))

        # 缩放（Qt.FastTransformation 优先保证性能）
        scaled = img.scaled(new_w, new_h, aspectRatioMode=1, transformMode=0)  # KeepAspectRatio, FastTransformation

        # 编码为 JPEG（相比 PNG 压缩率更高），质量 85 平衡体积与画质
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        try:
            if not scaled.save(buf, "JPEG", 85):
                # 回退 PNG
                buf.close()
                ba.clear()
                buf = QBuffer(ba)
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                if not scaled.save(buf, "PNG"):
                    logger.warning("[Vision] 图片压缩编码失败，返回原始图片")
                    return data_uri
        finally:
            buf.close()

        compressed_b64 = base64.b64encode(ba.data()).decode("utf-8")
        logger.warning(
            f"[Vision] 图片压缩完成: {orig_w}x{orig_h} → {new_w}x{new_h}, "
            f"base64: {len(b64_data)} → {len(compressed_b64)} bytes "
            f"({(1 - len(compressed_b64) / len(b64_data)) * 100:.1f}%)"
        )
        return f"data:image/jpeg;base64,{compressed_b64}"

    except Exception as e:
        logger.warning(f"[Vision] 图片压缩异常: {e}，返回原始图片")
        return data_uri


def compress_data_uri(data_uri: str, max_bytes: int = 5 * 1024 * 1024) -> str:
    """压缩 data URI 图片，确保 base64 数据不超过 max_bytes。

    使用 PyQt5 QImage（Python/PyInstaller 均可用）加载并等比缩小，
    避免依赖 PIL（PyInstaller 打包时已被移除）。

    Args:
        data_uri: 原始 data URI（如 data:image/png;base64,xxx）
        max_bytes: 压缩后 base64 部分的最大字节数（默认 5MB）

    Returns:
        压缩后的 data URI（若原图未超限则原样返回）；同一输入的压缩结果
        经 LRU 缓存（B1），第二轮起直接命中，不再重复解码/缩放/编码。
    """
    # 解析 data URI
    header_end = data_uri.find(",")
    if header_end == -1:
        return data_uri
    b64_data = data_uri[header_end + 1 :]

    # 检查是否需要压缩
    if len(b64_data) <= max_bytes:
        return data_uri

    digest = _data_uri_digest(data_uri)
    with _COMPRESS_CACHE_LOCK:
        cached = _COMPRESS_CACHE.get(digest)
        if cached is not None:
            _COMPRESS_CACHE.move_to_end(digest)
            return cached

    compressed = _compress_impl(data_uri, max_bytes)
    if compressed is not data_uri:
        # 只缓存「压缩确实发生」的结果；原样返回的路径下次走 b64 长度快检查即可
        with _COMPRESS_CACHE_LOCK:
            _COMPRESS_CACHE[digest] = compressed
            while len(_COMPRESS_CACHE) > _COMPRESS_CACHE_MAX:
                _COMPRESS_CACHE.popitem(last=False)
    return compressed


def revoke_vision_blocks(
    messages: List[Any],
    session_id: Any,
    root_dir: Optional[str] = None,
) -> int:
    """[方案 A] 发送后回收：把 data:image 块还原为 image_ref 引用块（就地修改）。

    persist 链（``_persist_vision_image_blocks_impl``）的逆操作的撤销端：
    API 请求结束后 data URI 无需常驻，还原为引用块，下轮 revive 幂等读回。

    还原判定（块特征匹配，不依赖索引对位）：
    - 未压缩图：现算 ``sha1(raw)[:16]`` 反推 persist 文件名，**fpath 存在性
      验证通过才还原**——任何反推错误（如压缩图被 LRU 驱逐后误判）都会因
      文件不存在而放弃还原，保留 data URI，不产生指向失效文件的 image_ref
    - 压缩图：摘要命中压缩 LRU（``compress_data_uri`` 缓存键）→ 保留 data
      URI（压缩结果 ≤5MB 且 LRU 上限 4 条，滞留有界 ≈20MB 上限）
    - 幂等：image_ref/其他类型块跳过，重复调用零操作

    Args:
        messages: 消息列表（就地修改）
        session_id: 落盘子目录名（persist 时的会话 ID）；str 或候选列表。
            子代理继承历史时原块 session_id 在 revive 后不可得，传候选集合
            逐目录探测（存在性验证兜底，全部不命中则安全降级保留）
        root_dir: 落盘根目录；缺省 get_app_data_dir()（与 revive 读侧同源）

    Returns:
        实际还原的块数
    """
    import base64 as _base64
    import hashlib as _hashlib

    if root_dir is None:
        from app.utils.utils import get_app_data_dir

        root_dir = get_app_data_dir()
    sid_list = [str(s or "") for s in (session_id if isinstance(session_id, (list, tuple, set)) else [session_id])]
    base_dirs = [Path(root_dir) / "screenshots" / "persisted" / s for s in sid_list]
    restored = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            img = block.get("image_url")
            url = img.get("url") if isinstance(img, dict) else None
            if not isinstance(url, str) or not url.startswith("data:image"):
                continue
            header_end = url.find(",")
            if header_end == -1:
                continue
            # 压缩图保留 data URI（摘要命中压缩 LRU 即认定经过压缩）
            with _COMPRESS_CACHE_LOCK:
                if _data_uri_digest(url) in _COMPRESS_CACHE:
                    continue
            try:
                raw = _base64.b64decode(url[header_end + 1 :])
                digest = _hashlib.sha1(raw).hexdigest()[:16]
            except Exception:  # noqa: BLE001 — 解码失败保留原块
                continue
            meta = url[5:header_end]  # "image/png;base64"，与 persist 写入口径同源
            ext = ".png" if "png" in meta else (".jpg" if "jpeg" in meta or "jpg" in meta else ".bin")
            fpath = None
            for base_dir in base_dirs:
                cand = base_dir / f"{digest}{ext}"
                try:
                    if cand.exists():
                        fpath = cand
                        break
                except OSError:
                    continue
            if fpath is None:
                continue  # 反推失败（含候选目录全不命中）→ 安全降级
            content[bi] = {
                "type": "image_ref",
                "image_ref": {
                    "file": fpath.name,
                    "meta": meta,
                    "session_id": fpath.parent.name,
                },
            }
            restored += 1
    return restored
