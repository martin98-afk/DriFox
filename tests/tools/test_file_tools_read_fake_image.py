# -*- coding: utf-8 -*-
"""
read 工具伪图防御回归测试

背景（2026-09-13）：工作区遗留 `.tmp_w11_nes.png` 实为 HTML 文件，read 按扩展名
标注 mime=image/png 走视觉注入，智谱解析失败报 1210「图片输入格式/解析错误」，
且注入图持久化进会话历史后每轮必发，会话被卡死。

修复：图片分支按 magic number 校验内容与扩展名是否相符，不符回退按文本读。

运行方式: python -m pytest tests/tools/test_file_tools_read_fake_image.py -v
"""
import base64
import os
import sys
from importlib import import_module
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ft = import_module("plugins.system-tools.tools.file_tools")


_MIN_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 8


def _read(tmp_path: Path, name: str, content: bytes):
    f = tmp_path / name
    f.write_bytes(content)
    ctx = {"workdir": str(tmp_path), "session_id": "t"}
    return ft._read_impl(tool_ctx=ctx, path=str(f))


def test_fake_png_falls_back_to_text(tmp_path):
    """HTML 内容伪装 .png：不返回 image_data，按文本读出原文"""
    html = b"<!DOCTYPE html>\n<html><body>err</body></html>\n"
    r = _read(tmp_path, "fake.png", html)
    assert r.success
    assert r.image_data is None, "伪图不得作为图片返回（防视觉注入污染）"
    assert "DOCTYPE html" in r.content


def test_real_png_still_returns_image_data(tmp_path):
    """真 PNG（magic 头正确）：维持原行为返回 image_data"""
    r = _read(tmp_path, "real.png", _MIN_PNG)
    assert r.success
    assert r.image_data is not None
    assert r.image_data["mime"] == "image/png"
    assert base64.b64decode(r.image_data["data"]) == _MIN_PNG


def test_fake_jpeg_and_webp_fall_back(tmp_path):
    """JPEG/WebP 扩展名同样受 magic 校验保护"""
    junk = b"not-an-image-at-all" * 4
    assert ft._read_impl(
        tool_ctx={"workdir": str(tmp_path), "session_id": "t"},
        path=str(tmp_path / "x.jpg"),
    )
    for name in ("x.jpg", "x.webp"):
        f = tmp_path / name
        f.write_bytes(junk)
    r1 = ft._read_impl(tool_ctx={"workdir": str(tmp_path), "session_id": "t"}, path="x.jpg")
    r2 = ft._read_impl(tool_ctx={"workdir": str(tmp_path), "session_id": "t"}, path="x.webp")
    assert r1.image_data is None and r2.image_data is None
    assert "not-an-image" in r1.content


def test_looks_like_image_table():
    """magic 表判定直测"""
    assert ft._looks_like_image(_MIN_PNG, ".png")
    assert not ft._looks_like_image(b"<!DOCTYPE html>", ".png")
    assert ft._looks_like_image(b"\xff\xd8\xff\xe0" + b"\x00" * 4, ".jpg")
    assert ft._looks_like_image(b"GIF89a" + b"\x00" * 4, ".gif")
    assert not ft._looks_like_image(b"BMxx", ".png")  # BMP 头对 .png 不算
