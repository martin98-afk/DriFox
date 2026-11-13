# -*- coding: utf-8 -*-
"""Gateway 入站媒体处理：图片压缩、文件清单、多模态内容构造。

分工：平台适配器只负责把图片/文件下载到本地缓存并塞进
``MessageEvent.media_urls``（``media_types`` 标注 image / file）；
本模块统一决定哪些内容直接交给模型、哪些降级为文件路径提示。

策略对齐 dsh-im：原文件留在磁盘，图片用压缩副本（长边 ≤2048、单张 ≤5MB、
总量 ≤20MB）；文本文件按预算内嵌原文，其余给路径让模型用文件工具读；
模型不支持视觉时图片也降级为路径提示，而不是让用户重发。
"""
from __future__ import annotations

import base64
import io
import os
from typing import Any, Dict, List, Optional, Tuple, Union

from loguru import logger

# 模型侧副本上限（原图不受限，只影响送进上下文的那份）
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 10
MAX_SIDE = 2048
_JPEG_QUALITIES = (85, 70, 55, 40)

# 文本文件内嵌预算（超出只给路径，避免一次消息撑爆上下文）
MAX_TEXT_FILE_BYTES = 32 * 1024
MAX_TEXT_TOTAL_BYTES = 64 * 1024

TEXT_FILE_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".less", ".sql", ".sh", ".bash", ".zsh",
    ".bat", ".ps1", ".xml", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
    ".rb", ".php", ".swift", ".kt", ".lua", ".r", ".m", ".tex", ".srt", ".vtt",
}

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

IMAGE_EXTENSIONS = set(_MIME_BY_EXT)

VISION_FALLBACK_HEADER = (
    "[用户随消息发送了图片，原图已保存在本机，路径见下。"
    "你无法直接看到图片内容，需要时用可用的文件读取/图像处理工具查看，不要假设自己已看到。]"
)


def _guess_mime(path: str) -> str:
    return _MIME_BY_EXT.get(os.path.splitext(path)[1].lower(), "image/png")


def _looks_like_image(raw: bytes) -> bool:
    """按魔数判真图片，避免把无效字节当图片送给模型。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"GIF8"):
        return True
    if raw.startswith(b"\xff\xd8\xff"):  # JPEG
        return True
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return True
    return raw[:2] == b"BM"  # BMP


def _read_raw(path: str, max_bytes: int) -> Optional[Tuple[bytes, str]]:
    try:
        with open(path, "rb") as f:
            raw = f.read(max_bytes + 1)
    except OSError as e:
        logger.debug(f"[GatewayMedia] 读取图片失败 {path}: {e}")
        return None
    if not raw or len(raw) > max_bytes or not _looks_like_image(raw):
        return None
    return raw, _guess_mime(path)


def _load_with_pillow(path: str, max_bytes: int) -> Optional[Tuple[bytes, str]]:
    try:
        from PIL import Image, ImageOps
    except Exception:
        return None

    try:
        with Image.open(path) as img:
            if getattr(img, "n_frames", 1) > 1:
                return _read_raw(path, max_bytes)
            img = ImageOps.exif_transpose(img)
            if os.path.getsize(path) <= max_bytes and max(img.width, img.height) <= MAX_SIDE:
                return _read_raw(path, max_bytes)
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            has_alpha = img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            )
            if has_alpha:
                buf = io.BytesIO()
                img.convert("RGBA").save(buf, format="PNG", optimize=True)
                raw = buf.getvalue()
                if len(raw) <= max_bytes:
                    return raw, "image/png"
            for quality in _JPEG_QUALITIES:
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
                raw = buf.getvalue()
                if len(raw) <= max_bytes:
                    return raw, "image/jpeg"
    except Exception as e:
        logger.debug(f"[GatewayMedia] Pillow 处理失败 {path}: {e}")
    return None


def _load_with_qt(path: str, max_bytes: int) -> Optional[Tuple[bytes, str]]:
    """无 Pillow 时的备选（主程序运行时不带 Pillow，靠 Qt 降采样）。"""
    try:
        from PyQt5.QtCore import QBuffer, QByteArray, QIODevice
        from PyQt5.QtGui import QImage
    except Exception:
        return None

    try:
        with open(path, "rb") as f:
            source = f.read()
        image = QImage.fromData(QByteArray(source))
        if image.isNull():
            return None
        # 尺寸与体积都达标时保留原字节，避免无意义的重编码
        if len(source) <= max_bytes and max(image.width(), image.height()) <= MAX_SIDE:
            return source, _guess_mime(path)
        scale = min(MAX_SIDE / max(image.width(), 1), MAX_SIDE / max(image.height(), 1), 1.0)
        if scale < 1.0:
            image = image.scaled(
                max(64, int(image.width() * scale)),
                max(64, int(image.height() * scale)),
                1,  # Qt.KeepAspectRatio
                1,  # Qt.SmoothTransformation
            )
        for quality in _JPEG_QUALITIES:
            payload = QByteArray()
            buffer = QBuffer(payload)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            try:
                ok = image.save(buffer, "JPEG", quality)
            finally:
                buffer.close()
            if ok and payload.size() and payload.size() <= max_bytes:
                return bytes(payload.data()), "image/jpeg"
    except Exception as e:
        logger.debug(f"[GatewayMedia] Qt 处理失败 {path}: {e}")
    return None


def _load_for_model(path: str, max_bytes: int) -> Optional[Tuple[bytes, str]]:
    """读图并按需压缩；返回 (bytes, mime)。超限或解码失败返回 None。"""
    loaded = _load_with_pillow(path, max_bytes)
    if loaded:
        return loaded
    loaded = _load_with_qt(path, max_bytes)
    if loaded:
        return loaded
    return _read_raw(path, max_bytes)


def _to_data_uri(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _normalize_kinds(paths: List[str], kinds: Optional[List[str]]) -> List[str]:
    """把 media_types 规整成与 paths 对齐的 image/file 序列。

    平台侧历来把 mime、文件名、自定义串塞进 media_types，这里按扩展名兜底：
    图片扩展名 → image，其余 → file。
    """
    result: List[str] = []
    for index, path in enumerate(paths):
        kind = str(kinds[index]).lower() if kinds and index < len(kinds) else ""
        if kind.startswith("image"):
            result.append("image")
        elif kind == "file":
            result.append("file")
        elif os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS:
            result.append("image")
        else:
            result.append("file")
    return result


def _read_text_excerpt(path: str, limit: int) -> Optional[str]:
    """读取文本文件片段；二进制（含 NUL）或读失败返回 None。"""
    try:
        with open(path, "rb") as f:
            raw = f.read(limit)
    except OSError as e:
        logger.debug(f"[GatewayMedia] 读取文件失败 {path}: {e}")
        return None
    if not raw or b"\x00" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def _build_files_text(files: List[str], unreadable_images: List[str]) -> str:
    """文件段落：清单 + 文本文件内嵌原文；图片失败项单列路径提示。"""
    sections: List[str] = []
    if files:
        sections.append("[用户随消息发送了以下文件]")
        for path in files:
            name = os.path.basename(path)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            sections.append(f"- {name}（{size} 字节，本地路径：{path}）")
        remaining = MAX_TEXT_TOTAL_BYTES
        for path in files:
            if remaining <= 0:
                break
            if os.path.splitext(path)[1].lower() not in TEXT_FILE_EXTENSIONS:
                continue
            excerpt = _read_text_excerpt(path, min(MAX_TEXT_FILE_BYTES, remaining))
            if excerpt is None:
                continue
            name = os.path.basename(path)
            sections.append(f"----- {name} 内容开始 -----\n{excerpt}\n----- {name} 内容结束 -----")
            remaining -= len(excerpt.encode("utf-8", "ignore"))
    if unreadable_images:
        sections.append(VISION_FALLBACK_HEADER)
        sections.extend(f"- {path}" for path in unreadable_images)
    return "\n".join(sections)


def build_user_content(
    text: str,
    paths: Optional[List[str]],
    *,
    supports_vision: bool,
    kinds: Optional[List[str]] = None,
) -> Union[str, List[Dict[str, Any]]]:
    """构造 user 消息 content：纯文本，或 [text, image_url...] 多模态列表。

    Args:
        text: 用户文本（可能为空）
        paths: 本地媒体路径（适配器下载后的缓存文件）
        supports_vision: 当前模型是否支持图像输入（resolve_model_capabilities 判定）
        kinds: 与 paths 对齐的类型标注（image / file），缺省按扩展名推断

    Returns:
        str（无图可发时）或 list（multimodal content）
    """
    valid = [p for p in (paths or []) if isinstance(p, str) and p and os.path.isfile(p)]
    if not valid:
        return text

    kinds_norm = _normalize_kinds(valid, kinds)
    images = [p for p, k in zip(valid, kinds_norm) if k == "image"]
    files = [p for p, k in zip(valid, kinds_norm) if k == "file"]

    image_blocks: List[Dict[str, Any]] = []
    unreadable_images: List[str] = []
    if images and supports_vision:
        used = 0
        for path in images[:MAX_IMAGES]:
            budget = min(MAX_IMAGE_BYTES, MAX_TOTAL_BYTES - used)
            if budget <= 0:
                unreadable_images.append(path)
                continue
            got = _load_for_model(path, budget)
            if not got:
                unreadable_images.append(path)
                continue
            raw, mime = got
            used += len(raw)
            image_blocks.append(
                {"type": "image_url", "image_url": {"url": _to_data_uri(raw, mime)}}
            )
    elif images:
        unreadable_images = list(images)

    lead = text or ("请分析这张图片。" if image_blocks else "")
    files_text = _build_files_text(files, unreadable_images)

    if image_blocks:
        blocks: List[Dict[str, Any]] = [{"type": "text", "text": lead}]
        blocks.extend(image_blocks)
        if files_text:
            blocks.append({"type": "text", "text": files_text})
        return blocks

    combined = "\n\n".join(part for part in (lead, files_text) if part)
    return combined or text
