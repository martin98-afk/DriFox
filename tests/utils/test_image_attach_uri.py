# -*- coding: utf-8 -*-
"""_image_path_to_data_uri 骨架测试（#2.8 清单 1）"""
import base64
import os
import tempfile

from app.main_widget import _image_path_to_data_uri


# 最小合法 PNG（1x1 透明像素，67 字节）
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def test_tiny_png_returns_data_uri():
    """<8MB 小 PNG 应直接编码返回 data URI。"""
    png_bytes = base64.b64decode(_TINY_PNG_B64)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png_bytes)
        tmp_path = f.name
    try:
        result = _image_path_to_data_uri(tmp_path)
        assert result is not None
        assert result.startswith("data:image/png;base64,")
        assert len(result) > 30
    finally:
        os.unlink(tmp_path)


def test_nonexistent_file_returns_none():
    """不存在的文件应返回 None（不抛异常）。"""
    result = _image_path_to_data_uri("/nonexistent/path/to/image.png")
    assert result is None