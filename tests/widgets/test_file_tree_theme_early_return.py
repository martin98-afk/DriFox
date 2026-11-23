# -*- coding: utf-8 -*-
"""file-tree `_CodeView.apply_theme` 函数级早退回归

覆盖：
- 同参连续调用只真正生效一次（setFont / setStyleSheet / set_theme 各 1）
- 只改文字颜色：仅 setStyleSheet +1，不重设字体
- 只改字号：仅 setFont +1，不重设样式表
- Ctrl+滚轮缩放后再 apply_theme：字号回落到主题默认（补救 A）
- 选区 alpha 70→120 必须重上样式表（HexArgb 快照；HexRgb 会把它视为同色）
- extra_qss / gutter 颜色 / is_dark 变化都要触发样式更新
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication

_QAPP = QApplication.instance() or QApplication(sys.argv)

_PLUGIN_UI = Path(__file__).resolve().parent.parent.parent / "plugins" / "file-tree" / "ui"


def _load_package():
    if "file_tree_theme_ui" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "file_tree_theme_ui", _PLUGIN_UI / "__init__.py", submodule_search_locations=[str(_PLUGIN_UI)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_theme_ui"] = pkg
    spec.loader.exec_module(pkg)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package()
preview_mod = _load_module("file_tree_theme_ui.preview", _PLUGIN_UI / "preview.py")

_CodeView = preview_mod._CodeView


def _sel(alpha: int) -> QColor:
    c = QColor(80, 150, 220)
    c.setAlpha(alpha)
    return c


def _args(**over):
    kw = dict(
        text_color=QColor(220, 220, 220),
        gutter_text=QColor(150, 150, 150),
        gutter_bg=QColor(255, 255, 255, 14),
        selection=_sel(70),
        font_pixel_size=13,
        extra_qss="QScrollBar:vertical { width: 6px; }",
        is_dark=True,
    )
    kw.update(over)
    return kw


@pytest.fixture()
def view():
    """_CodeView 实例 + 三个写操作的调用计数"""
    v = _CodeView()
    counts = {"setFont": 0, "setStyleSheet": 0, "set_theme": 0}
    real_setfont = v.setFont
    real_setstylesheet = v.setStyleSheet
    real_settheme = v._highlighter.set_theme

    def spy_setfont(*a, **kw):
        counts["setFont"] += 1
        return real_setfont(*a, **kw)

    def spy_setstylesheet(*a, **kw):
        counts["setStyleSheet"] += 1
        return real_setstylesheet(*a, **kw)

    def spy_settheme(*a, **kw):
        counts["set_theme"] += 1
        return real_settheme(*a, **kw)

    v.setFont = spy_setfont
    v.setStyleSheet = spy_setstylesheet
    v._highlighter.set_theme = spy_settheme
    yield v, counts
    v.deleteLater()


def test_same_args_three_times_only_once(view):
    """a) 同参连调 3 次：setFont / setStyleSheet / set_theme 各 1"""
    v, counts = view
    for _ in range(3):
        v.apply_theme(**_args())
    assert counts == {"setFont": 1, "setStyleSheet": 1, "set_theme": 1}
    assert "#465096dc" in v.styleSheet()  # selection alpha 70 → HexArgb #46…
    assert v.font_pixel_size() == 13


def test_text_color_change_only_touches_stylesheet(view):
    """b) 改文字颜色：只 setStyleSheet +1，setFont 不动"""
    v, counts = view
    v.apply_theme(**_args())
    before = dict(counts)
    v.apply_theme(**_args(text_color=QColor(200, 100, 100)))
    assert counts["setStyleSheet"] == before["setStyleSheet"] + 1
    assert counts["setFont"] == before["setFont"]
    assert counts["set_theme"] == before["set_theme"]
    assert "color: #c86464" in v.styleSheet()
    assert v.font_pixel_size() == 13


def test_font_size_change_only_touches_font(view):
    """c) 改字号：只 setFont +1，setStyleSheet 不动"""
    v, counts = view
    v.apply_theme(**_args())
    before = dict(counts)
    v.apply_theme(**_args(font_pixel_size=20))
    assert counts["setFont"] == before["setFont"] + 1
    assert counts["setStyleSheet"] == before["setStyleSheet"]
    assert v.font_pixel_size() == 20


def test_zoom_then_apply_theme_restores_default_px(view):
    """d) Ctrl+滚轮缩放后同参 apply_theme 必须把字号拉回主题默认"""
    v, counts = view
    v.apply_theme(**_args(font_pixel_size=13))
    v.set_font_pixel_size(28)
    assert v.font_pixel_size() == 28
    before = dict(counts)
    # 主题参数一个字都没变，只有 _font_px 被滚轮改过
    v.apply_theme(**_args(font_pixel_size=13))
    assert v.font_pixel_size() == 13
    assert counts["setFont"] == before["setFont"] + 1
    assert counts["setStyleSheet"] == before["setStyleSheet"]  # 样式没变不重设


def test_selection_alpha_change_is_not_swallowed(view):
    """e) 选区 alpha 70→120 必须重上样式表；HexRgb 快照看不出差别"""
    v, counts = view
    # 先证明只比 HexRgb 确实会串色
    assert _sel(70).name(QColor.HexRgb) == _sel(120).name(QColor.HexRgb)
    assert _sel(70).name(QColor.HexArgb) != _sel(120).name(QColor.HexArgb)

    v.apply_theme(**_args(selection=_sel(70)))
    assert "#465096dc" in v.styleSheet()
    assert counts["setStyleSheet"] == 1

    v.apply_theme(**_args(selection=_sel(120)))
    assert counts["setStyleSheet"] == 2
    assert "#785096dc" in v.styleSheet()
    assert "#465096dc" not in v.styleSheet()
    assert counts["setFont"] == 1  # 只改选区不该重置字体


@pytest.mark.parametrize(
    "over",
    [
        {"extra_qss": "QScrollBar:vertical { width: 10px; }"},
        {"gutter_text": QColor(180, 180, 180)},
        {"gutter_bg": QColor(255, 255, 255, 40)},
        {"is_dark": False},
    ],
    ids=["extra_qss", "gutter_text", "gutter_bg", "is_dark"],
)
def test_style_inputs_each_trigger_refresh(view, over):
    """样式侧任一输入变化都必须重上样式表，不能被签名吞掉"""
    v, counts = view
    v.apply_theme(**_args())
    before = dict(counts)
    v.apply_theme(**_args(**over))
    assert counts["setStyleSheet"] == before["setStyleSheet"] + 1
    # 高亮配色只跟明暗走：只有 is_dark 变化才重跑词法着色
    expected_theme_delta = 1 if "is_dark" in over else 0
    assert counts["set_theme"] == before["set_theme"] + expected_theme_delta


def test_first_call_always_applies(view):
    """冷启动一次都不能漏：__init__ 后第一调必须落地"""
    v, counts = view
    assert v._theme_sig is None
    v.apply_theme(**_args())
    assert counts == {"setFont": 1, "setStyleSheet": 1, "set_theme": 1}
    assert v._theme_sig is not None
