# -*- coding: utf-8 -*-
"""图像编解码工具（与对话/工作线程解耦，供 store 层复用）。

从 ``app.core.workers.chat_worker`` 抽出：``revive_vision_image_refs``（store 层）
需要压缩超大 data URI，直接反向 import workers 会形成 store → workers 的反向依赖。
"""

from loguru import logger
from PyQt5.QtCore import QBuffer, QByteArray, QIODevice
from PyQt5.QtGui import QImage


def compress_data_uri(data_uri: str, max_bytes: int = 5 * 1024 * 1024) -> str:
    """压缩 data URI 图片，确保 base64 数据不超过 max_bytes。

    使用 PyQt5 QImage（Python/PyInstaller 均可用）加载并等比缩小，
    避免依赖 PIL（PyInstaller 打包时已被移除）。

    Args:
        data_uri: 原始 data URI（如 data:image/png;base64,xxx）
        max_bytes: 压缩后 base64 部分的最大字节数（默认 5MB）

    Returns:
        压缩后的 data URI（若原图未超限则原样返回）
    """
    # 解析 data URI
    header_end = data_uri.find(",")
    if header_end == -1:
        return data_uri
    b64_data = data_uri[header_end + 1 :]

    # 检查是否需要压缩
    if len(b64_data) <= max_bytes:
        return data_uri

    try:
        import base64

        img_bytes = base64.b64decode(b64_data)
        img = QImage.fromData(QByteArray(img_bytes))
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
