# -*- coding: utf-8 -*-
"""GatewayEngine 多模态/文件入参单测：内容怎么进会话、视觉能力如何降级。

不构造 QObject（用 __new__ 跳过 __init__），只验证纯逻辑分支。
"""
import pytest

from app.core.engines.gateway.engine import GatewayEngine


@pytest.fixture()
def engine():
    return GatewayEngine.__new__(GatewayEngine)


@pytest.fixture()
def image_file(tmp_path):
    path = tmp_path / "shot.png"
    try:
        from PIL import Image

        Image.new("RGB", (32, 32), (10, 200, 90)).save(path)
    except ImportError:
        from PyQt5.QtGui import QColor, QImage

        image = QImage(32, 32, QImage.Format_RGB32)
        image.fill(QColor(10, 200, 90))
        assert image.save(str(path))
    return str(path)


@pytest.fixture()
def text_file(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# 标题\n正文内容", encoding="utf-8")
    return str(path)


def test_no_media_keeps_plain_text(engine):
    assert engine._build_user_content("你好", [], [], {}) == "你好"
    assert engine._build_user_content("你好", None, None, {}) == "你好"


def test_vision_model_gets_multimodal_content(engine, image_file):
    content = engine._build_user_content(
        "看图", [image_file], ["image"], {"模型名称": "vision-model", "声明_支持图像": True}
    )
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"


def test_non_vision_model_degrades_to_text(engine, image_file):
    content = engine._build_user_content(
        "看图", [image_file], ["image"], {"模型名称": "text-model", "声明_支持图像": False}
    )
    assert isinstance(content, str)
    assert image_file in content
    assert "无法直接看到图片" in content


def test_text_file_content_is_inlined(engine, text_file):
    content = engine._build_user_content("看文件", [text_file], ["file"], {})
    assert isinstance(content, str)
    assert "正文内容" in content
    assert text_file in content


def test_image_and_file_mixed(engine, image_file, text_file):
    content = engine._build_user_content(
        "两个都看",
        [image_file, text_file],
        ["image", "file"],
        {"模型名称": "vision-model", "声明_支持图像": True},
    )
    assert isinstance(content, list)
    kinds = [block["type"] for block in content]
    assert kinds.count("image_url") == 1
    assert any("正文内容" in str(block.get("text", "")) for block in content)
