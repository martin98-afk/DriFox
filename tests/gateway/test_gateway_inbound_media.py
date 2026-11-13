# -*- coding: utf-8 -*-
"""Gateway 入站图片与文本切分单测。

覆盖两处新增逻辑：
- ``system_im_core.text_split.split_message``：自然边界优先、不切出孤片
- ``system_im_core.media.build_user_content``：多模态构造与不支持视觉时的降级
"""
import base64
import os

import pytest

from system_im_core.media import VISION_FALLBACK_HEADER, build_user_content
from system_im_core.text_split import split_message


# ==================== 文本切分 ====================

def test_split_short_text_returns_single_chunk():
    assert split_message("短消息", 1800) == ["短消息"]


def test_split_empty_text_returns_empty_list():
    assert split_message("", 1800) == []
    assert split_message("   \n ", 1800) == []


def test_split_prefers_newline_and_keeps_limit():
    body = "\n".join(f"第{i}行内容" for i in range(1000))
    chunks = split_message(body, 1800)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1800 for chunk in chunks)
    # 按行切分时不应把某行拆成两半
    assert all("第" in chunk and "行内容" in chunk for chunk in chunks)


def test_split_falls_back_to_space_when_no_newline():
    chunks = split_message("word " * 800, 1800)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1800 for chunk in chunks)
    assert all(not chunk.startswith(" ") and not chunk.endswith(" ") for chunk in chunks)


def test_split_hard_cuts_when_no_boundary():
    chunks = split_message("x" * 4000, 1800)
    assert [len(chunk) for chunk in chunks] == [1800, 1800, 400]


def test_split_zero_limit_returns_original():
    assert split_message("abc", 0) == ["abc"]


def test_split_prefers_paragraph_boundary():
    body = "第一段" * 120 + "\n\n" + "第二段" * 120
    chunks = split_message(body, 500, prefer_paragraph=True)
    assert len(chunks) == 2
    assert chunks[0].startswith("第一段")
    assert chunks[1].startswith("第二段")


# ==================== 入站图片 ====================

@pytest.fixture()
def image_file(tmp_path):
    """生成一张真实图片（优先 Pillow，缺失时退回 Qt），返回路径。"""
    path = tmp_path / "shot.png"
    try:
        from PIL import Image

        Image.new("RGB", (64, 48), (30, 120, 200)).save(path)
        return str(path)
    except ImportError:
        pass
    from PyQt5.QtGui import QColor, QImage

    image = QImage(64, 48, QImage.Format_RGB32)
    image.fill(QColor(30, 120, 200))
    assert image.save(str(path))
    return str(path)


def test_no_images_returns_plain_text():
    assert build_user_content("你好", [], supports_vision=True) == "你好"
    assert build_user_content("你好", None, supports_vision=True) == "你好"


def test_missing_file_falls_back_to_text():
    content = build_user_content("看图", ["/nonexistent/x.png"], supports_vision=True)
    assert content == "看图"


def test_vision_disabled_degrades_to_path_listing(image_file):
    content = build_user_content("看图", [image_file], supports_vision=False)
    assert isinstance(content, str)
    assert image_file in content
    assert VISION_FALLBACK_HEADER in content


def test_vision_enabled_builds_multimodal_blocks(image_file):
    content = build_user_content("看图", [image_file], supports_vision=True)
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看图"}
    assert content[1]["type"] == "image_url"
    uri = content[1]["image_url"]["url"]
    assert uri.startswith("data:image/")
    assert base64.b64decode(uri.split(",", 1)[1])


def test_empty_text_with_image_gets_default_prompt(image_file):
    content = build_user_content("", [image_file], supports_vision=True)
    assert isinstance(content, list)
    assert content[0]["text"]


def test_corrupt_image_degrades_to_text(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    content = build_user_content("看图", [str(broken)], supports_vision=True)
    # 解码失败 → 退回文本清单，消息不丢
    assert isinstance(content, str)
    assert str(broken) in content
