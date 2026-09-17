# -*- coding: utf-8 -*-
"""身份行控件：头像三态（图片 / 内置图标 / 首字母色块）、pixmap 缓存、左右对齐。

说明：Qt 控件的真实绘制细节（字体、DPI、实际布局）需真实应用验证，
本文件只锁定「能被 offscreen 稳定断言的」契约。
"""

import pytest
from PIL import Image as PILImage
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import QApplication

from app.core.message_identity import BUILTIN_AVATAR_DRIFOX, MessageIdentity
from app.widgets.modules import identity_header as ih


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _clean_avatar_cache():
    ih.clear_avatar_cache()
    yield
    ih.clear_avatar_cache()


def test_initials_for_latin_and_cjk():
    assert ih._initials("dingmama") == "D"
    assert ih._initials("测试助手") == "测"
    assert ih._initials("") == "?"
    assert ih._initials("   ") == "?"
    # 跳过分隔符取首个有效字符
    assert ih._initials("-hanako") == "H"


def test_color_for_name_is_stable_and_in_palette():
    first = ih._color_for_name("hanako")
    assert first == ih._color_for_name("hanako")
    assert first in ih._PALETTE
    assert ih._color_for_name("") == ih._PALETTE[0]
    # 不同名字通常会落到不同色（不强制，但至少都在调色板内）
    assert ih._color_for_name("build") in ih._PALETTE


def test_qcolor_parses_rgba_and_rejects_invalid():
    """QColor 不认 rgba() 字符串且不抛异常——必须显式解析，否则画成黑块。"""
    fallback = ih.QColor(1, 2, 3)
    parsed = ih._qcolor("rgba(255, 128, 0, 200)", fallback)
    assert (parsed.red(), parsed.green(), parsed.blue(), parsed.alpha()) == (255, 128, 0, 200)
    assert ih._qcolor("", fallback) == fallback
    assert ih._qcolor("not-a-color", fallback) == fallback
    # 合法 hex 正常透传
    assert ih._qcolor("#7C3AED", fallback).isValid()


def test_avatar_pixmap_builtin_icon(qapp):
    pixmap = ih.resolve_avatar_pixmap(MessageIdentity(name="Drifox", avatar=BUILTIN_AVATAR_DRIFOX))
    assert pixmap is not None and not pixmap.isNull()


def test_avatar_pixmap_cached_by_reference(qapp):
    identity = MessageIdentity(name="Drifox", avatar=BUILTIN_AVATAR_DRIFOX)
    first = ih.resolve_avatar_pixmap(identity)
    second = ih.resolve_avatar_pixmap(identity)
    assert first is second


def test_avatar_pixmap_missing_file_returns_none(qapp):
    identity = MessageIdentity(name="hanako", avatar="C:/definitely/not/here.png")
    assert ih.resolve_avatar_pixmap(identity) is None


def test_avatar_pixmap_empty_avatar_returns_none(qapp):
    assert ih.resolve_avatar_pixmap(MessageIdentity(name="hanako")) is None


def test_avatar_pixmap_uses_high_quality_resample(qapp, tmp_path):
    """大比例降采样必须走高质量重采样，而非 Qt 一步 SmoothTransformation。

    回归守卫：250×250 → 32×32 是 7.8:1，Qt 单步双线性高频丢失明显（肉眼发虚）。
    `_hq_square` 走 Pillow LANCZOS，实测边缘能量 stddev 92.9 → 99.8（+7%）。
    这里锁定「输出为正方形 + 尺寸正确 + 与 Qt 路径结果不同」，
    锐度绝对值受源图影响，不在单测中断言。
    """
    src = tmp_path / "big.png"
    PILImage.new("RGB", (256, 256)).save(src)
    img = PILImage.open(src).convert("RGB")
    # 高频棋盘格：任何插值损失都会让输出偏离纯黑白
    pixels = img.load()
    for y in range(256):
        for x in range(256):
            v = 0 if ((x // 8) + (y // 8)) % 2 else 255
            pixels[x, y] = (v, v, v)
    img.save(src)

    pm = ih.resolve_avatar_pixmap(MessageIdentity(name="checker", avatar=str(src)), 32)
    assert pm is not None and not pm.isNull()
    assert pm.width() == pm.height() == 32

    qt_way = QPixmap(str(src)).scaled(32, 32, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    assert pm.toImage() != qt_way.toImage(), "高质量缩放未生效（与 Qt 默认缩放输出完全一致）"


def test_hq_square_falls_back_when_pillow_missing(qapp, monkeypatch, tmp_path):
    """Pillow 不可用时必须回落（ok=False），绝不返回坏图。"""
    src = tmp_path / "a.png"
    PILImage.new("RGBA", (64, 64), (255, 0, 0, 255)).save(src)
    pixmap = QPixmap(str(src))
    monkeypatch.setattr(ih, "_pil_resample", lambda: None)
    result, ok = ih._hq_square(pixmap, 32, 1.0)
    assert ok is False and result is None


def test_avatar_renders_full_image_on_hidpi(qapp, monkeypatch, tmp_path):
    """HiDPI（dpr>1）下头像必须画满整张源图，不能只剩左上 1/4。

    回归守卫（2026-09-17 用户反馈）：QPixmap.width() 返回**设备像素**，
    paintEvent 曾拿它与**逻辑** self._size 混算偏移与源矩形边长，
    dpr=2 时只取到源图左上 32×32 设备像素 = 源图 1/4。
    现走 drawPixmap(目标矩形, pixmap, 源矩形) 显式区分两套单位。

    断言方式：四象限异色源图，渲染结果（圆内）四个象限颜色都必须出现。
    """
    src = tmp_path / "quad.png"
    img = PILImage.new("RGBA", (64, 64))
    pixels = img.load()
    for y in range(64):
        for x in range(64):
            if y < 32:
                pixels[x, y] = (255, 0, 0, 255) if x < 32 else (0, 255, 0, 255)
            else:
                pixels[x, y] = (0, 0, 255, 255) if x < 32 else (255, 255, 0, 255)
    img.save(src)

    monkeypatch.setattr(ih, "_device_pixel_ratio", lambda: 2.0)
    widget = ih.IdentityAvatar(MessageIdentity(name="q", avatar=str(src)), 32)
    assert widget._pixmap.devicePixelRatio() == 2.0

    grabbed = widget.grab()
    qimg = grabbed.toImage().convertToFormat(QImage.Format_RGBA8888)
    width, height = qimg.width(), qimg.height()
    bits = qimg.constBits()
    bits.setsize(height * width * 4)
    rendered = PILImage.frombytes("RGBA", (width, height), bytes(bits))

    # 只统计圆形头像内部（避开圆裁外的透明角）
    cx = cy = width / 2
    radius_sq = (width / 2 - 2) ** 2
    seen = set()
    for y in range(height):
        for x in range(width):
            if (x - cx) ** 2 + (y - cy) ** 2 > radius_sq:
                continue
            r, g, b, a = rendered.getpixel((x, y))
            if a < 200 or max(r, g, b) < 120:
                continue
            top, left = y < height / 2, x < width / 2
            if top and left and r > 150:
                seen.add("左上")
            elif top and not left and g > 150:
                seen.add("右上")
            elif not top and left and b > 150:
                seen.add("左下")
            elif not top and not left and r > 150 and g > 150:
                seen.add("右下")
    assert seen == {"左上", "右上", "左下", "右下"}, f"HiDPI 下头像未画满整图，实际可见象限：{sorted(seen)}"


def test_identity_avatar_renders_without_image(qapp):
    """无头像时回落色块 + 首字母，不能崩。"""
    widget = ih.IdentityAvatar(MessageIdentity(name="hanako"))
    widget.grab()  # 触发 paintEvent
    assert widget.size().width() == ih.AVATAR_SIZE


def test_identity_header_aligns_and_updates(qapp):
    identity = MessageIdentity(name="hanako")
    left = ih.IdentityHeader(identity, align_right=False)
    right = ih.IdentityHeader(identity, align_right=True)
    assert left._name_label.text() == "hanako"
    assert right._name_label.text() == "hanako"

    left.set_identity(MessageIdentity(name="build"))
    assert left._name_label.text() == "build"

    left.apply_text_color("#ff0000")
    assert "#ff0000" in left._name_label.styleSheet()


def test_header_has_fixed_height(qapp):
    """身份行高度固定：卡片高度上报走 HeightCommitBatch，不能让身份行抖动。"""
    header = ih.IdentityHeader(MessageIdentity(name="测试"))
    assert header.height() >= ih.AVATAR_SIZE + 2
    # 固定高：多次 resize 不变
    header.resize(300, header.height())
    header.show()
    qapp.processEvents()
    assert header.height() == header.maximumHeight() == header.minimumHeight()


def test_timestamp_row_fully_visible(qapp):
    """第二行（时间）必须完整落在身份行可视区内。

    回归守卫：QLabel 默认 sizeHint 高含内边距（实测 30px），两行需求 60px 会
    超出身份行固定高 → 时间被推到可视区外，表现为「时间只显示一瞬间就消失」
    （2026-09-16 用户反馈的真因）。现显式压缩标签高度并在布局高度上留余量。
    """
    header = ih.IdentityHeader(MessageIdentity(name="mading"), align_right=True, timestamp="09-16 23:05")
    header.resize(220, header.height())
    header.show()
    qapp.processEvents()

    name, time = header._name_label, header._time_label
    assert time.isVisible()
    assert time.text() == "09-16 23:05"
    # 两行不重叠
    assert time.y() >= name.y() + name.height(), f"时间与名称重叠：name={name.geometry()}, time={time.geometry()}"
    # 时间底边不超出身份行
    assert time.y() + time.height() <= header.height(), (
        f"时间被裁：time bottom={time.y() + time.height()} > header h={header.height()}"
    )


def test_name_label_font_and_size(qapp):
    """名称的字族与字号必须真实生效。

    两个踩过的坑（2026-09-16 走查）：
    1. `font_size_css()` 返回的已是完整声明（`font-size: 20px;`），再生拼一层
       `font-size:` → `font-size: font-size: 20px;;`，非法 CSS 被 Qt 静默丢弃、
       字号停在默认值。现改走 `setFont()`，QSS 不再出现 font-size。
    2. QSS 的 `font-family` 解析不到中文字体名（`'楷体'`/`楷体`/`KaiTi` 全落到宋体），
       而 `QFont("楷体")` 能正确匹配。故字族必须走 `setFont()`。
    """
    from PyQt5.QtGui import QFontInfo

    header = ih.IdentityHeader(MessageIdentity(name="mading"), align_right=True)
    label = header._name_label

    # 字号：跟随全局字号档位缩放
    assert label.font().pixelSize() == ih.scale_font_size(ih.NAME_FONT_SIZE)
    assert label.font().bold()

    # 字族：必须落到用户配置的字体，而非 QSS 回落的宋体
    from app.utils.utils import Settings

    expected = Settings.get_instance().llm_font_family.value
    assert QFontInfo(label.font()).family() == expected, (
        f"名称字族应为 {expected!r}，实际 {QFontInfo(label.font()).family()!r}"
    )

    # QSS 里不应再出现字号/字族声明（它们由 setFont 负责）
    css = label.styleSheet()
    assert "font-size" not in css, f"字号不应走 QSS（会被非法拼接或忽略）: {css!r}"
    assert "font-family" not in css, f"字族不应走 QSS（中文名解析不到）: {css!r}"

    # 主题刷新路径同样保持（apply_text_color 只改颜色）
    header.apply_text_color("#8FA4C2")
    css2 = label.styleSheet()
    assert "color: #8FA4C2" in css2
    assert "font-size" not in css2
    assert label.font().pixelSize() == ih.scale_font_size(ih.NAME_FONT_SIZE)
