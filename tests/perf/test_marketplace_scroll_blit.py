# -*- coding: utf-8 -*-
"""插件市场列表滚动性能回归测试（marketscroll blit 优化）。

背景（实测，原生 Windows 平台，215 行 / 视口 1000x577）：
    QScrollArea 滚动时走 QWidgetPrivate::scrollRect 位块传输（blit）：已绘制
    区域整块搬移，只重绘新露出的一条。该优化要求「滚动内容 widget 自身不
    透明」，否则 Qt 无法判断搬移后残留像素是否需要与下层背景合成，会回退
    到「全视口重绘」。

    修复前：``_content`` 设 ``background: transparent`` → 每滚动一步触发
    ~106 次 paint（视口内 6 行 × 17 个子控件全部重绘），paint 耗时 p50 1.9ms。
    修复后：``_content`` 走 palette 实底（autoFillBackground）→ 每步 ~20 次
    paint（仅新露出的一行），paint 耗时 p50 0.6ms。滚动成本与总行数解耦
    （600 行时仍为 20 次/步）。

关键点（易被改回去）：
    1. 必须用 **palette + autoFillBackground**，不能用 QSS。QSS 背景由
       QStyleSheetStyle 在 paintEvent 内绘制，不改写 QWidget 的 opaque
       判定 → blit 仍被禁用（实测 106 次/步，与透明时一致）。
    2. 底色 alpha 必须为 255（``color.setAlpha(255)``）：半透明同样禁用 blit。
    3. 调用点必须落在 ``_scroll.setWidget(...)`` 与 ``SmoothScrollDelegate``
       安装之后：setWidget 会 reparent content 到视口，实测紧跟其设置仍会被
       清掉（构造结束后读回 autoFillBackground=False）。

本测试只做静态分析（读源码文本 + re 匹配），不实例化 GUI、不需要显示器。
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "plugins" / "plugin-marketplace" / "ui" / "cards.py"


@pytest.fixture(scope="module")
def src_text() -> str:
    return SRC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def surface_fn_body(src_text: str) -> str:
    """取 _apply_content_surface_color 函数体"""
    start = src_text.find("def _apply_content_surface_color(self):")
    assert start != -1, "未找到 MarketplaceCard._apply_content_surface_color 定义"
    end = src_text.find("\n    def ", start + 1)
    return src_text[start:end]


def test_content_no_longer_uses_transparent_qss(src_text: str):
    """内容层不得再用 QSS 声明透明底 —— QSS 背景无法让 Qt 判定 widget 不透明。"""
    assert 'self._content.setStyleSheet("background: transparent;")' not in src_text, (
        "_content 不应再用 QSS 设透明底（会禁用滚动 blit 优化，滚动退回全视口重绘）"
    )


def test_surface_uses_palette_autofill(surface_fn_body: str):
    """底色必须走 palette + autoFillBackground（QSS 无效）。"""
    assert "setAutoFillBackground(True)" in surface_fn_body, "_apply_content_surface_color 必须开启 autoFillBackground"
    assert ".setPalette(" in surface_fn_body, "底色必须写入 palette，而不是 setStyleSheet"
    assert "setStyleSheet" not in surface_fn_body, "QSS 背景不改写 opaque 判定，不能用于此处"


def test_surface_color_is_fully_opaque(surface_fn_body: str):
    """底色 alpha 必须强制 255：半透明同样会禁用滚动 blit。"""
    assert "setAlpha(255)" in surface_fn_body, "内容层底色必须全不透明（alpha=255），否则 blit 优化失效"


def test_surface_color_follows_theme(surface_fn_body: str):
    """底色取上下文 content_bg，主题切换时随之刷新。"""
    assert 'get("content_bg")' in surface_fn_body, "底色应取上下文 colors.content_bg"
    assert "_cached_theme_colors" in surface_fn_body, "底色应复用已缓存的上下文色表"


def test_surface_applied_after_scroll_widget_installed(src_text: str):
    """调用点必须在 setWidget / SmoothScrollDelegate 之后（否则属性被清掉）。"""
    # 必须用 MarketplaceCard 自己的 _setup_ui：文件里还有其它类的同名方法
    i_cls = src_text.find("class MarketplaceCard(QWidget):")
    i_setup = src_text.find("    def _setup_ui(self):", i_cls)
    i_setwidget = src_text.find("self._scroll.setWidget(self._content)")
    i_delegate = src_text.find("SmoothScrollDelegate(self._scroll)")
    assert i_cls != -1 and i_setup != -1, "未找到 MarketplaceCard._setup_ui 定义"
    # 只取 _setup_ui 内的调用（主题分支里另有一次调用，不参与装配顺序判定）
    i_apply = src_text.find("self._apply_content_surface_color()", i_setup)
    assert i_setwidget != -1 and i_delegate != -1 and i_apply != -1, "未找到滚动区装配或底色设置调用"
    assert i_apply > i_setwidget, "底色必须在 setWidget 之后设置（setWidget 会 reparent 并重置属性）"
    assert i_apply > i_delegate, "底色必须在 SmoothScrollDelegate 安装之后设置"


def test_surface_refreshed_on_theme_change(src_text: str):
    """主题变化分支必须重新刷底色，否则换主题后仍是旧色。"""
    m = re.search(
        r"if theme_changed:\n((?:[ ]{12,}.*\n)+?)",
        src_text,
    )
    assert m is not None, "未找到 `if theme_changed:` 分支"
    branch = m.group(1)
    assert "_apply_content_surface_color()" in branch, "主题变化时必须同步刷新内容层底色"
