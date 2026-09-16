# -*- coding: utf-8 -*-
"""身份行控件：头像三态（图片 / 内置图标 / 首字母色块）、pixmap 缓存、左右对齐。

说明：Qt 控件的真实绘制细节（字体、DPI、实际布局）需真实应用验证，
本文件只锁定「能被 offscreen 稳定断言的」契约。
"""

import pytest
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
