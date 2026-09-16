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
    assert header.height() == ih.AVATAR_SIZE + 2


def test_name_label_font_size_is_valid_css(qapp):
    """名称字号必须是合法 CSS。

    回归守卫：`font_size_css()` 返回的就已是完整声明（`font-size: 20px;`），
    再生拼一层 `font-size:` 会得到 `font-size: font-size: 20px;;` —— 非法 CSS，
    Qt 静默丢弃整条声明，字号永远停在默认值（2026-09-16 走查发现的真实 bug）。
    """
    header = ih.IdentityHeader(MessageIdentity(name="mading"), align_right=True)
    css = header._name_label.styleSheet()
    assert css.count("font-size:") == 1, f"font-size 声明必须只出现一次，实际: {css!r}"
    assert ih.font_size_css(ih.NAME_FONT_SIZE) in css

    # 主题刷新路径同样校验（apply_text_color 会重写样式）
    header.apply_text_color("#8FA4C2")
    css2 = header._name_label.styleSheet()
    assert css2.count("font-size:") == 1, f"着色后 font-size 声明异常: {css2!r}"
    assert "color: #8FA4C2" in css2
