# -*- coding: utf-8 -*-
"""出站内容与平台入参的解析工具（自 ``app/core/conversation/backend.py`` 迁入）。

- ``_extract_markdown_images`` / ``_extract_markdown_files``：从模型回复的
  Markdown 里摘出本地图片/附件路径并清掉标记，交给平台适配器原生发送；
- ``_gw_str_platform``：平台入参归一（枚举内平台转 ``Platform``，第三方
  ``str`` 平台 id 原样直通，Phase E 契约）。

宿主侧不再使用这些函数，保留在插件内避免主程序背负 IM 语义。
"""
from __future__ import annotations

import os
import re
from typing import Any

from app.constants import IMAGE_EXTENSIONS
from app.gateway.base import Platform as GatewayPlatform


def _extract_markdown_images(content: str) -> tuple[str, list[str]]:
    """从 Markdown 内容中提取本地图片文件路径。

    检测 ``![alt](path)`` 语法，只提取本地存在的图片文件。
    远程 URL、不存在的文件、非图片文件均不提取。

    Returns:
        (clean_content, image_paths) - 清理后的文本和本地图片路径列表
    """
    image_paths: list[str] = []

    def _replace_img(match: re.Match) -> str:
        path = match.group(1).strip()
        if os.path.isfile(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                image_paths.append(path)
                return ""  # 移除图片标记
        return match.group(0)  # 保留原样

    cleaned = re.sub(r"!\[.*?\]\((.+?)\)", _replace_img, content)
    return cleaned, image_paths


def _extract_markdown_files(content: str) -> tuple[str, list[str]]:
    """从 Markdown 内容中提取本地附件文件路径（非图片）。

    检测 ``[name](path)`` 语法（含 ``📎 [name](path)`` 形态），只提取本地存在、
    且扩展名不在 ``IMAGE_EXTENSIONS`` 的普通文件。远程 URL 与图片保留原样，
    交给 ``_extract_markdown_images`` 处理。

    Returns:
        (clean_content, file_paths) - 清理后的文本和本地文件路径列表
    """
    file_paths: list[str] = []

    def _replace_link(match: re.Match) -> str:
        path = match.group(2).strip()
        if os.path.isfile(path):
            ext = os.path.splitext(path)[1].lower()
            if ext not in IMAGE_EXTENSIONS:
                file_paths.append(path)
                return ""  # 移除链接标记
        return match.group(0)  # 保留原样

    cleaned = re.sub(r"(?<!!)\[([^\]]*)\]\((.+?)\)", _replace_link, content)
    return cleaned, file_paths


def _gw_str_platform(platform: Any):
    """Gateway 平台入参归一：枚举内平台转 Platform；第三方 str 平台 id 原样直通。"""
    if isinstance(platform, GatewayPlatform):
        return platform
    try:
        return GatewayPlatform(platform)
    except ValueError:
        return platform
